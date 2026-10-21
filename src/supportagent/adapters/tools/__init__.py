"""First-party read tools (ARCH §7, 11-wp WP-107): logs, airflow, gha, cron, oncall.

Every tool is ``read`` / T0 and declares its result schema. Arguments
arrive already bound from the case's catalog service (``ToolRegistry``).
The change-feed tool (11-wp: exactly one of Argo CD, Terraform Cloud,
Consul or ServiceNow change requests) lands once the design partner picks
one (CAP §6 item 7, BL-038).
"""
