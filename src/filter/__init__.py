"""Phase 19 filter package.

Houses the per-posting analysis cache (``score_cache``) and the
fast-path that consults A1 snapshot tags + A2 score cache before
falling back to the existing ``src.matching`` scorer.
"""
