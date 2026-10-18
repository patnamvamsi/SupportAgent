"""Persistence repositories (10-wp): one module per 0001 table family.

Repositories are thin, typed wrappers over the Core tables; they take the
active ``SqlUnitOfWork`` and run on its connection, so they participate in
the unit of work's transaction (commit-on-demand, roll-back otherwise).
"""
