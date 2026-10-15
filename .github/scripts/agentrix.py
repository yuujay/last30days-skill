"""Trusted Actions client. No repository code or returned artifact is executed."""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

AGENT = '47e876f2-5c7d-486b-9ade-fc5f93f5d1ed'
AGENT_VERSION = '1.0.0'
WORKFLOW_VERSION = 'v1'
REPOSITORY = 'yuujay/last30days-skill'
SUMMARY_MARKER = '<!-- code-review -->'


class APIError(Exception):
    def __init__(self, service, status):
        super().__init__(f'{service} request failed (HTTP {status})')
        self.status = status


class AmbiguousPublication(Exception):
    def __init__(self):
        super().__init__('Publication remains ambiguous; reconcile the saved intent before another write.')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, token, method='GET', body=None, service='github'):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('HTTPS endpoint required')
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'}
    data = None if body is None else json.dumps(body, ensure_ascii=True).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=35) as response:
            content = response.read(8 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise APIError(service, exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise APIError(service, 0) from None
    if len(content) > 8 * 1024 * 1024:
        raise APIError(service, 0)
    try:
        return json.loads(content) if content else {}
    except ValueError:
        raise APIError(service, 0) from None


class Client:
    def __init__(self):
        self.base = os.environ['AGENTRIX_URL'].rstrip('/')
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme != 'https' or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('Invalid Agentrix endpoint')
        self.repository_id = os.environ['GITHUB_REPOSITORY_ID']
        if os.environ['GITHUB_REPOSITORY'] != REPOSITORY:
            raise ValueError('Repository not allowed')
        self.oidc = None
        self.oidc_at = 0

    def api(self, path, method='GET', body=None):
        if time.monotonic() - self.oidc_at > 120 or self.oidc is None:
            url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
            separator = '&' if '?' in url else '?'
            response = request(url + separator + 'audience=agentrix-pr-review',
                               os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN'], service='oidc')
            self.oidc = response['value']
            self.oidc_at = time.monotonic()
        return request(self.base + path, self.oidc, method, body, 'agentrix')

    def github(self, path, method='GET', body=None):
        return request('https://api.github.com' + path, os.environ['GITHUB_TOKEN'], method, body)

    def pages(self, path):
        items = []
        for page in range(1, 11):
            batch = self.github(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            if not isinstance(batch, list):
                raise ValueError('Invalid GitHub page')
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise ValueError('GitHub pagination limit reached; publication requires complete reconciliation')


def is_bot(item, bot_id):
    author = item.get('user') or {}
    return author.get('id') == bot_id and author.get('login') == 'github-actions[bot]' and author.get('type') == 'Bot'


def marker_for(result):
    task = result['task']
    return f"<!-- agentrix:task={task['id']};head={task['submission']['headSha']};result={result['digest']} -->"


def bounded_summary(summary, marker, notices):
    footer = '\n\n' + '\n'.join(notices) + '\n' + marker
    if len(summary) + len(footer) > 65536:
        footer = '\n\nSummary truncated; full findings remain in Agentrix artifacts.' + footer
        summary = summary[:65536 - len(footer)]
    return summary + footer


def reconciled_write(attempted, reconcile, intent, write):
    found = reconcile()
    if found is not None:
        return found
    if attempted:
        raise AmbiguousPublication()
    intent()
    try:
        return write()
    except APIError as exc:
        found = reconcile()
        if found is not None:
            return found
        if 400 <= exc.status < 500 and exc.status not in (408, 429):
            raise
        raise AmbiguousPublication() from None


def submit(client):
    with open(os.environ['GITHUB_EVENT_PATH'], encoding='utf-8') as stream:
        event = json.load(stream)
    if os.environ['GITHUB_EVENT_NAME'] != 'pull_request_target' or event.get('action') != 'opened':
        raise ValueError('Only PR-opening events are accepted')
    pr = event['pull_request']
    if str(event['repository']['id']) != client.repository_id or event['repository']['full_name'] != REPOSITORY:
        raise ValueError('Event repository mismatch')
    # The request fingerprint must survive workflow reruns. At PR opening there
    # are no earlier Agentrix threads; any retrieval limitation is explicit.
    payload = {'repositoryId': client.repository_id, 'repository': REPOSITORY, 'pr': pr['number'],
               'baseSha': pr['base']['sha'], 'headSha': pr['head']['sha'], 'baseRef': 'refs/heads/' + pr['base']['ref'],
               'title': pr['title'], 'description': (pr.get('body') or '')[:60000], 'author': pr['user']['login'],
               'agent': AGENT, 'agentVersion': AGENT_VERSION, 'workflowVersion': WORKFLOW_VERSION,
               'contextLimitation': 'Prior review threads are not included in the PR-opening payload; duplicate detection is limited.'}
    response = client.api('/pr-reviews', 'POST', payload)
    task_id = response['task']['id']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task_id):
        raise ValueError('Invalid task identity')
    print(f'Accepted Agentrix task {task_id}; remote execution continues independently.')


def validate_result(client, result):
    task, manifest = result['task'], result['manifest']
    submission = task['submission']
    if (submission['repositoryId'] != client.repository_id or submission['repository'] != REPOSITORY
            or submission['agent'] != AGENT or submission['agentVersion'] != AGENT_VERSION
            or submission['workflowVersion'] != WORKFLOW_VERSION
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task['id'])
            or not re.fullmatch(r'[a-f0-9]{40}', submission['headSha'])
            or not re.fullmatch(r'[a-f0-9]{64}', result['digest'])
            or type(submission['pr']) is not int or submission['pr'] < 1
            or manifest['attempt']['taskId'] != task['id'] or manifest['attempt']['headSha'] != submission['headSha']
            or manifest['repositoryId'] != client.repository_id or manifest['repository'] != REPOSITORY
            or manifest['pr'] != submission['pr'] or task['state'] not in ('completed', 'incomplete', 'failed')
            or not result['summary'].startswith(SUMMARY_MARKER + '\n')):
        raise ValueError('Result identity mismatch')


def publish_one(client, task_id, bot_id):
    endpoint = '/pr-reviews/' + task_id
    result = client.api(endpoint + '/result')['result']
    validate_result(client, result)
    submission = result['task']['submission']
    if result['manifest']['coverage']['emptyDiff']:
        raise ValueError('Empty diff must already be skipped')
    lease = client.api(endpoint + '/publication/claim', 'POST', {'digest': result['digest']})['lease']
    # Read progress after the claim: a previous owner may have persisted a write.
    result = client.api(endpoint + '/result')['result']
    validate_result(client, result)
    progress = result['publication']
    marker = marker_for(result)
    root = f"/repos/{REPOSITORY}"
    pr_path = f"{root}/pulls/{submission['pr']}"
    issue_path = f"{root}/issues/{submission['pr']}"
    receipt = {'digest': result['digest'], 'headSha': submission['headSha'], 'outcome': 'published',
               'inlineStatus': 'none', 'reviewId': 0, 'commentId': 0}
    notices = []

    def current():
        latest = client.api(endpoint)['task']
        if latest['state'] == 'canceled':
            raise ValueError('Task canceled before publication')
        pr = client.github(pr_path)
        if str(pr['base']['repo']['id']) != client.repository_id:
            raise ValueError('GitHub repository identity changed')
        if pr['state'] != 'open':
            return 'closed'
        if pr['head']['sha'] != submission['headSha']:
            return 'superseded'
        return None

    def acknowledge():
        client.api(endpoint + '/publication/ack', 'POST', {'leaseId': lease['id'], 'receipt': receipt})

    def intent(phase):
        client.api(endpoint + '/publication/intent', 'POST', {'leaseId': lease['id'], 'phase': phase})

    def find_review():
        matches = [r for r in client.pages(pr_path + '/reviews') if is_bot(r, bot_id)
                   and marker in (r.get('body') or '') and r.get('commit_id') == submission['headSha']]
        if len(matches) > 1:
            raise AmbiguousPublication()
        return matches[0] if matches else None

    def find_summary():
        matches = [c for c in client.pages(issue_path + '/comments') if is_bot(c, bot_id)
                   and marker in (c.get('body') or '') and SUMMARY_MARKER in (c.get('body') or '')]
        if len(matches) > 1:
            raise AmbiguousPublication()
        return matches[0] if matches else None

    stale = current()
    if stale:
        # Reconcile any effect from an interrupted earlier run before skipping.
        prior_review, prior_summary = find_review(), find_summary()
        receipt.update(outcome=stale, reviewId=prior_review['id'] if prior_review else 0,
                       commentId=prior_summary['id'] if prior_summary else 0, inlineStatus='superseded')
        acknowledge()
        return

    inline_object = client.api(endpoint + '/artifacts/inline.json')['content']
    ref = result['manifest']['artifacts']['inline.json']
    if hashlib.sha256(inline_object.encode()).hexdigest() != ref['digest'] or len(inline_object.encode()) != ref['size']:
        raise ValueError('Inline artifact checksum mismatch')
    lines = json.loads(inline_object)
    comments = []
    for finding in result['findings']:
        if (finding['side'] == 'RIGHT' and finding['path'] in lines
                and type(finding['line']) is int and finding['line'] in lines[finding['path']]):
            comments.append({'path': finding['path'], 'line': finding['line'], 'side': 'RIGHT',
                             'body': f"**[{finding['severity']}] {finding['title']}**\n\n{finding['body']}"})
    if len(comments) > 50:
        notices.append('Inline comments limited to 50; all findings are retained in the saved summary and artifacts.')
        comments = comments[:50]
    if comments:
        try:
            review = reconciled_write(progress['reviewAttempted'], find_review,
                                      lambda: intent('reviewAttempted'),
                                      lambda: client.github(pr_path + '/reviews', 'POST',
                                          {'commit_id': submission['headSha'], 'event': 'COMMENT',
                                           'body': marker, 'comments': comments}))
            receipt.update(reviewId=review['id'], inlineStatus='published')
            urls = client.pages(pr_path + f"/reviews/{review['id']}/comments")
            for comment in urls:
                url = comment.get('html_url', '')
                if is_bot(comment, bot_id) and url.startswith(f'https://github.com/{REPOSITORY}/pull/{submission["pr"]}#'):
                    notices.append(f"Inline finding: {url}")
        except APIError as exc:
            if exc.status not in (400, 403, 404, 422):
                raise
            receipt['inlineStatus'] = 'failed'
            notices.append('Inline publication failed; findings remain in this summary and the saved artifacts.')
    stale = current()
    if stale:
        receipt.update(outcome=stale)
        acknowledge()
        return
    body = bounded_summary(result['summary'], marker, notices)

    def write_summary():
        existing = [c for c in client.pages(issue_path + '/comments') if is_bot(c, bot_id)
                    and SUMMARY_MARKER in (c.get('body') or '')]
        if existing:
            return client.github(root + f"/issues/comments/{existing[-1]['id']}", 'PATCH', {'body': body})
        return client.github(issue_path + '/comments', 'POST', {'body': body})

    summary = reconciled_write(progress['summaryAttempted'], find_summary,
                               lambda: intent('summaryAttempted'), write_summary)
    receipt['commentId'] = summary['id']
    # GitHub cannot lock the head across calls. Record a concurrent change.
    stale = current()
    if stale:
        receipt['outcome'] = stale
    acknowledge()
    print(f"Published saved Agentrix result for PR {submission['pr']} (task {task_id}).")


def publish(client):
    bot = client.github('/users/github-actions%5Bbot%5D')
    if bot['login'] != 'github-actions[bot]' or bot['type'] != 'Bot':
        raise ValueError('Cannot establish publishing identity')
    tasks = client.api('/pr-review-publications?limit=5')['tasks']
    failures = 0
    for task in tasks:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task['id']):
            raise ValueError('Invalid pending task identity')
        try:
            publish_one(client, task['id'], bot['id'])
        except APIError as exc:
            if exc.status == 409:
                continue  # Another unexpired owner has the publication lease.
            failures += 1
            print(f"Task {task['id']}: {exc}", file=sys.stderr)
        except (AmbiguousPublication, ValueError) as exc:
            failures += 1
            # Values and artifacts must not appear in diagnostic messages.
            print(f"Task {task['id']}: {type(exc).__name__}; result remains available for recovery.", file=sys.stderr)
    if failures:
        raise ValueError('Some publications require recovery')
    print(f'Processed {len(tasks)} ready result(s); exiting without waiting for execution.')


if __name__ == '__main__':
    try:
        client = Client()
        {'submit': submit, 'publish': publish}[sys.argv[1]](client)
    except Exception as exc:
        print(f'Agentrix client failed: {type(exc).__name__}. Credentials and response bodies omitted.', file=sys.stderr)
        sys.exit(1)
