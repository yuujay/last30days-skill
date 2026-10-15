import importlib.util
from pathlib import Path
import unittest
import copy
import hashlib
import json

spec = importlib.util.spec_from_file_location('agentrix', Path(__file__).with_name('agentrix.py'))
agentrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agentrix)


class PublicationTests(unittest.TestCase):
    def test_marker_and_summary_bound(self):
        result = {'task': {'id': 'task123', 'submission': {'headSha': 'b' * 40}}, 'digest': 'd' * 64}
        marker = agentrix.marker_for(result)
        summary = agentrix.bounded_summary('<!-- code-review -->\n' + 'x' * 80000, marker, ['Inline publication failed.'])
        self.assertLessEqual(len(summary), 65536)
        self.assertTrue(summary.startswith('<!-- code-review -->'))
        self.assertIn(marker, summary)
        self.assertIn('truncated', summary)
        self.assertIn('Inline publication failed.', summary)

    def test_bot_identity_is_required(self):
        self.assertTrue(agentrix.is_bot({'user': {'id': 41898282, 'login': 'github-actions[bot]', 'type': 'Bot'}}, 41898282))
        self.assertFalse(agentrix.is_bot({'user': {'id': 9, 'login': 'someone', 'type': 'User'}}, 41898282))

    def test_lost_response_is_not_blindly_retried(self):
        calls = []
        def write():
            calls.append('write')
            raise agentrix.APIError('github', 503)
        with self.assertRaises(agentrix.AmbiguousPublication):
            agentrix.reconciled_write(True, lambda: None, lambda: calls.append('intent'), write)
        self.assertEqual(calls, [])
        with self.assertRaises(agentrix.AmbiguousPublication):
            agentrix.reconciled_write(False, lambda: None, lambda: calls.append('intent'), write)
        self.assertEqual(calls, ['intent', 'write'])
        self.assertEqual(agentrix.reconciled_write(True, lambda: {'id': 42}, lambda: None, write), {'id': 42})

    def test_publication_uses_saved_result_and_comment_review(self):
        client = FakeClient()
        agentrix.publish_one(client, 'task123', 41898282)
        self.assertEqual(len(client.reviews), 1)
        self.assertEqual(client.reviews[0]['event'], 'COMMENT')
        self.assertEqual(client.reviews[0]['commit_id'], 'b' * 40)
        self.assertEqual(client.receipt['outcome'], 'published')
        self.assertEqual(len(client.comments), 2)
        self.assertEqual(client.comments[0]['body'], '<!-- code-review -->\nUser-authored comment')
        self.assertLessEqual(len(client.comments[1]['body']), 65536)

    def test_lost_github_response_reconciles_without_duplicate(self):
        client = FakeClient()
        client.lose_response = True
        agentrix.publish_one(client, 'task123', 41898282)
        agentrix.publish_one(client, 'task123', 41898282)
        self.assertEqual(len(client.reviews), 1)
        self.assertEqual(len(client.comments), 2)

    def test_stale_head_suppresses_writes(self):
        client = FakeClient()
        client.head = 'c' * 40
        agentrix.publish_one(client, 'task123', 41898282)
        self.assertEqual(client.reviews, [])
        self.assertEqual(len(client.comments), 1)
        self.assertEqual(client.receipt['outcome'], 'superseded')

    def test_push_between_phases_retains_actual_effect(self):
        client = FakeClient()
        client.push_after_review = True
        agentrix.publish_one(client, 'task123', 41898282)
        self.assertEqual(len(client.reviews), 1)
        self.assertEqual(len(client.comments), 1)
        self.assertEqual(client.receipt['outcome'], 'superseded')
        self.assertEqual(client.receipt['reviewId'], 10)


class FakeClient:
    def __init__(self):
        self.repository_id = '123'
        self.head = 'b' * 40
        self.lose_response = False
        self.push_after_review = False
        self.bot = {'id': 41898282, 'login': 'github-actions[bot]', 'type': 'Bot'}
        self.comments = [{'id': 99, 'body': '<!-- code-review -->\nUser-authored comment', 'user': {'id': 55, 'login': 'human', 'type': 'User'}}]
        self.reviews = []
        self.inline = json.dumps({'a.go': [2]})
        self.receipt = None
        self.result = {'task': {'id': 'task123', 'state': 'completed', 'submission': {
            'repositoryId': '123', 'repository': agentrix.REPOSITORY, 'pr': 1,
            'headSha': 'b' * 40, 'agent': agentrix.AGENT, 'agentVersion': agentrix.AGENT_VERSION,
            'workflowVersion': agentrix.WORKFLOW_VERSION}},
            'manifest': {'repositoryId': '123', 'repository': agentrix.REPOSITORY, 'pr': 1,
                         'attempt': {'taskId': 'task123', 'headSha': 'b' * 40}, 'coverage': {'emptyDiff': False},
                         'artifacts': {'inline.json': {'digest': hashlib.sha256(self.inline.encode()).hexdigest(), 'size': len(self.inline)}}},
            'digest': 'd' * 64, 'summary': '<!-- code-review -->\n## Code Review\nA verified finding.',
            'publication': {'reviewAttempted': False, 'summaryAttempted': False},
            'findings': [{'path': 'a.go', 'line': 2, 'side': 'RIGHT', 'severity': 'high', 'title': 'Regression', 'body': 'Concrete evidence.'}]}

    def api(self, path, method='GET', body=None):
        if path.endswith('/result'):
            return {'result': copy.deepcopy(self.result)}
        if path.endswith('/claim'):
            return {'lease': {'id': 'lease'}}
        if path.endswith('/intent'):
            self.result['publication'][body['phase']] = True
            return {'accepted': True}
        if path.endswith('/ack'):
            self.receipt = body['receipt']
            return {'accepted': True}
        if path.endswith('/artifacts/inline.json'):
            return {'content': self.inline}
        return {'task': self.result['task']}

    def pages(self, path):
        if '/reviews/' in path:
            return []
        if path.endswith('/reviews'):
            return self.reviews
        return self.comments

    def github(self, path, method='GET', body=None):
        if method == 'GET':
            return {'state': 'open', 'head': {'sha': self.head}, 'base': {'repo': {'id': 123}}}
        if path.endswith('/reviews'):
            review = dict(body, id=10, user=self.bot)
            self.reviews.append(review)
            if self.push_after_review:
                self.head = 'c' * 40
            if self.lose_response:
                raise agentrix.APIError('github', 503)
            return review
        comment = dict(body, id=100, user=self.bot)
        self.comments.append(comment)
        if self.lose_response:
            raise agentrix.APIError('github', 503)
        return comment


if __name__ == '__main__':
    unittest.main()
