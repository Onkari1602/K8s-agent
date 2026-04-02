import logging
import threading
import time
from datetime import datetime, timezone

from kubernetes import client as k8s_client, config as k8s_config, watch

from . import config
from .models import PodIssue, PodState
from .analyzer import BedrockAnalyzer
from .remediation import Remediator
from .reporter import Reporter
from .continuous_optimizer import ContinuousOptimizer, COST_INTERVAL
from .spot_manager import SpotManager
from .persistent_store import PersistentStore

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("healing-agent")


class HealingAgent:
    def __init__(self):
        self._init_k8s()
        self.analyzer = BedrockAnalyzer()
        self.remediator = Remediator(self.apps_v1, self.core_v1)
        self.reporter = Reporter(self.core_v1)
        self.optimizer = ContinuousOptimizer(self.core_v1, self.apps_v1)
        self.spot_manager = SpotManager(self.core_v1, self.apps_v1)
        self.dedup_tracker = {}

        # Persistent store (DynamoDB)
        try:
            self.store = PersistentStore()
            logger.info("  DynamoDB persistent store: connected")
        except Exception as e:
            logger.warning(f"  DynamoDB unavailable, using in-memory: {e}")
            self.store = None

    def _init_k8s(self):
        try:
            k8s_config.load_incluster_config()
            logger.info("Loaded in-cluster K8s config")
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
            logger.info("Loaded local kubeconfig")
        self.core_v1 = k8s_client.CoreV1Api()
        self.apps_v1 = k8s_client.AppsV1Api()

    def run(self):
        logger.info("=" * 60)
        logger.info("K8s Self-Healing Agent started")
        logger.info(f"  Monitor interval: {config.MONITOR_INTERVAL}s")
        logger.info(f"  Namespaces: {config.MONITOR_NAMESPACES or 'ALL (except excluded)'}")
        logger.info(f"  Excluded: {config.EXCLUDE_NAMESPACES}")
        logger.info(f"  Auto-remediate: {config.AUTO_REMEDIATE}")
        logger.info(f"  Dry-run: {config.DRY_RUN}")
        logger.info(f"  Bedrock model: {config.BEDROCK_MODEL_ID}")
        logger.info(f"  Bedrock endpoint: {config.BEDROCK_ENDPOINT_URL or 'public'}")
        logger.info("=" * 60)

        watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        watch_thread.start()

        optimizer_thread = threading.Thread(target=self._optimizer_loop, daemon=True)
        optimizer_thread.start()
        logger.info(f"  Cost optimizer: every {COST_INTERVAL}s")

        self.spot_manager.start_background()
        logger.info("  Spot manager: monitoring interruptions")

        self._polling_loop()

    def _watch_loop(self):
        w = watch.Watch()
        while True:
            try:
                namespaces = self._get_namespaces()
                for ns in namespaces:
                    logger.debug(f"Starting watch on namespace: {ns}")
                    try:
                        for event in w.stream(
                            self.core_v1.list_namespaced_pod,
                            namespace=ns,
                            timeout_seconds=300,
                        ):
                            if event["type"] in ("MODIFIED", "ADDED"):
                                pod = event["object"]
                                issue = self._detect_issue(pod)
                                if issue and self._should_process(issue):
                                    self._handle_issue(issue)
                    except Exception as e:
                        logger.warning(f"Watch error on {ns}: {e}")
            except Exception as e:
                logger.error(f"Watch loop error: {e}")
            time.sleep(5)

    def _optimizer_loop(self):
        """Background thread for continuous cost optimization."""
        time.sleep(60)  # Wait for agent to fully start
        while True:
            try:
                self.optimizer.run_cycle()
            except Exception as e:
                logger.error(f"Optimizer loop error: {e}")
            time.sleep(COST_INTERVAL)

    def _polling_loop(self):
        while True:
            try:
                self._scan_cluster()
                self.reporter.cleanup_old_reports()
            except Exception as e:
                logger.error(f"Polling loop error: {e}")
            time.sleep(config.MONITOR_INTERVAL)

    def _scan_cluster(self):
        namespaces = self._get_namespaces()
        total_issues = 0

        for ns in namespaces:
            try:
                pods = self.core_v1.list_namespaced_pod(ns)
                for pod in pods.items:
                    issue = self._detect_issue(pod)
                    if issue and self._should_process(issue):
                        total_issues += 1
                        self._handle_issue(issue)
            except Exception as e:
                logger.error(f"Error scanning namespace {ns}: {e}")

        if total_issues > 0:
            logger.info(f"Scan complete: {total_issues} issues found")

    def _get_namespaces(self):
        if config.MONITOR_NAMESPACES:
            return config.MONITOR_NAMESPACES
        ns_list = self.core_v1.list_namespace()
        return [
            ns.metadata.name for ns in ns_list.items
            if ns.metadata.name not in config.EXCLUDE_NAMESPACES
        ]

    def _detect_issue(self, pod) -> PodIssue | None:
        labels = pod.metadata.labels or {}
        for key, val in config.PROTECTED_LABELS.items():
            if labels.get(key) == val:
                return None

        if pod.status.phase == "Running":
            if not pod.status.container_statuses:
                return None
            all_ready = all(cs.ready for cs in pod.status.container_statuses)
            if all_ready:
                return None

        state = PodState.UNKNOWN
        reason = ""
        message = ""
        container_name = None
        restart_count = 0

        if pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                restart_count = max(restart_count, cs.restart_count or 0)

                if cs.state and cs.state.waiting:
                    r = cs.state.waiting.reason or ""
                    if r == "CrashLoopBackOff":
                        state = PodState.CRASH_LOOP_BACK_OFF
                    elif r in ("ImagePullBackOff", "ErrImagePull"):
                        state = PodState.IMAGE_PULL_BACK_OFF
                    else:
                        state = PodState.ERROR
                    reason = r
                    message = cs.state.waiting.message or ""
                    container_name = cs.name
                    break

                if cs.state and cs.state.terminated:
                    r = cs.state.terminated.reason or ""
                    if r == "OOMKilled":
                        state = PodState.OOM_KILLED
                    elif r == "Error":
                        state = PodState.ERROR
                    reason = r
                    message = cs.state.terminated.message or ""
                    container_name = cs.name

                if cs.last_state and cs.last_state.terminated:
                    r = cs.last_state.terminated.reason or ""
                    if r == "OOMKilled":
                        state = PodState.OOM_KILLED
                        reason = r
                        container_name = cs.name

        if state == PodState.UNKNOWN and pod.status.phase == "Pending":
            state = PodState.PENDING
            if pod.status.conditions:
                for cond in pod.status.conditions:
                    if cond.type == "PodScheduled" and cond.status == "False":
                        reason = cond.reason or "Unschedulable"
                        message = cond.message or ""

        if state in (PodState.UNKNOWN,):
            return None

        owner_kind, owner_name = self._get_owner(pod)
        logs = self._get_logs(pod.metadata.name, pod.metadata.namespace, container_name)
        events = self._get_events(pod.metadata.name, pod.metadata.namespace)
        requests, limits = self._get_resources(pod, container_name)

        return PodIssue(
            pod_name=pod.metadata.name,
            namespace=pod.metadata.namespace,
            node_name=pod.spec.node_name,
            state=state,
            reason=reason,
            message=message,
            container_name=container_name,
            restart_count=restart_count,
            owner_kind=owner_kind,
            owner_name=owner_name,
            logs_tail=logs,
            events=events,
            resource_requests=requests,
            resource_limits=limits,
        )

    def _should_process(self, issue: PodIssue) -> bool:
        key = f"{issue.namespace}/{issue.pod_name}/{issue.state.value}"
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()

        # Check DynamoDB first, fallback to in-memory
        if self.store:
            last_ts = self.store.get_dedup(key)
            if last_ts > 0 and (now_ts - last_ts) < config.DEDUP_COOLDOWN_SECONDS:
                return False
            self.store.set_dedup(key, now_ts)
        else:
            if key in self.dedup_tracker:
                last_processed = self.dedup_tracker[key]
                elapsed = (now - last_processed).total_seconds()
                if elapsed < config.DEDUP_COOLDOWN_SECONDS:
                    return False
            self.dedup_tracker[key] = now

        return True

    def _handle_issue(self, issue: PodIssue):
        logger.info(f"Issue detected: {issue.namespace}/{issue.pod_name} - {issue.state.value} ({issue.reason})")

        analysis = self.analyzer.analyze_issue(issue)
        logger.info(f"Analysis: root_cause='{analysis.root_cause}' action={analysis.recommended_action.value} confidence={analysis.confidence:.2f}")

        report = self.remediator.execute(analysis)
        logger.info(f"Remediation: action={report.action_taken.value} success={report.success}")

        self.reporter.generate_report(report)

        # Log to DynamoDB for audit trail
        if self.store:
            self.store.log_action(
                action_type=report.action_taken.value,
                target=f"{issue.namespace}/{issue.pod_name}",
                details=f"{analysis.root_cause} | {report.details}",
                success=report.success,
            )

    def _get_owner(self, pod):
        if pod.metadata.owner_references:
            for ref in pod.metadata.owner_references:
                if ref.kind == "ReplicaSet":
                    try:
                        rs = self.apps_v1.read_namespaced_replica_set(ref.name, pod.metadata.namespace)
                        if rs.metadata.owner_references:
                            for rs_ref in rs.metadata.owner_references:
                                if rs_ref.kind == "Deployment":
                                    return "Deployment", rs_ref.name
                    except Exception:
                        pass
                    return "ReplicaSet", ref.name
                return ref.kind, ref.name
        return None, None

    def _get_logs(self, pod_name, namespace, container_name=None):
        try:
            kwargs = {"name": pod_name, "namespace": namespace, "tail_lines": 50}
            if container_name:
                kwargs["container"] = container_name
            return self.core_v1.read_namespaced_pod_log(**kwargs)
        except Exception:
            try:
                kwargs["previous"] = True
                return self.core_v1.read_namespaced_pod_log(**kwargs)
            except Exception:
                return None

    def _get_events(self, pod_name, namespace):
        try:
            events = self.core_v1.list_namespaced_event(
                namespace,
                field_selector=f"involvedObject.name={pod_name}",
            )
            return [
                f"[{e.type}] {e.reason}: {e.message}"
                for e in sorted(events.items, key=lambda x: x.last_timestamp or x.first_timestamp or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:10]
            ]
        except Exception:
            return []

    def _get_resources(self, pod, container_name):
        requests = {}
        limits = {}
        if pod.spec.containers:
            for c in pod.spec.containers:
                if container_name and c.name != container_name:
                    continue
                if c.resources:
                    if c.resources.requests:
                        requests = dict(c.resources.requests)
                    if c.resources.limits:
                        limits = dict(c.resources.limits)
                break
        return requests, limits


def main():
    agent = HealingAgent()
    agent.run()


if __name__ == "__main__":
    main()
