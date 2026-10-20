"""supportagent.adapters.webhook (01 §1 layout; 11-wp WP-113).

The generic webhook path needs no adapter of its own: signature and window
checks are pure (``domain.webhook``), and the nonce store and inbox are
database repositories (``adapters.db.repos.webhooks``). Adapters may not
import each other (``adapters-independent``). The package stays as the home
for transports with their own I/O (e.g. mTLS client-certificate checks).
"""
