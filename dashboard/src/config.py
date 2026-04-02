import os

# Monitoring
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "30"))
MONITOR_NAMESPACES = [ns.strip() for ns in os.getenv("MONITOR_NAMESPACES", "").split(",") if ns.strip()]
EXCLUDE_NAMESPACES = [ns.strip() for ns in os.getenv("EXCLUDE_NAMESPACES", "kube-system,kube-public,kube-node-lease").split(",")]

# Bedrock
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "ap-south-1")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-sonnet-20240229-v1:0")
BEDROCK_ENDPOINT_URL = os.getenv("BEDROCK_ENDPOINT_URL", "")
BEDROCK_MAX_TOKENS = int(os.getenv("BEDROCK_MAX_TOKENS", "4096"))

# Remediation
AUTO_REMEDIATE = os.getenv("AUTO_REMEDIATE", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.7"))
OOM_MEMORY_INCREASE_PERCENT = int(os.getenv("OOM_MEMORY_INCREASE_PERCENT", "50"))
OOM_MEMORY_MAX_MI = int(os.getenv("OOM_MEMORY_MAX_MI", "4096"))
MAX_ROLLBACK_REVISIONS = int(os.getenv("MAX_ROLLBACK_REVISIONS", "3"))
CRASHLOOP_RESTART_THRESHOLD = int(os.getenv("CRASHLOOP_RESTART_THRESHOLD", "5"))
DEDUP_COOLDOWN_SECONDS = int(os.getenv("DEDUP_COOLDOWN_SECONDS", "600"))

# Reporting
REPORT_STORAGE = os.getenv("REPORT_STORAGE", "configmap")
REPORT_S3_BUCKET = os.getenv("REPORT_S3_BUCKET", "")
REPORT_NAMESPACE = os.getenv("REPORT_NAMESPACE", "atlas")
REPORT_RETENTION_DAYS = int(os.getenv("REPORT_RETENTION_DAYS", "30"))

# Safety
PROTECTED_NAMESPACES = ["kube-system", "kube-public", "kube-node-lease"]
PROTECTED_LABELS = {"self-healing": "disabled"}

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
