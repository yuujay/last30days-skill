"""Intentional review fixture for Agentrix validation. Do not merge this PR."""


def success_rate(completed: int, total: int) -> float:
    """Return the percentage completed, or 0.0 when the batch is empty."""
    return 100.0 * completed / total
