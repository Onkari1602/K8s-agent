import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import boto3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from kubernetes import client as k8s_client, config as k8s_config

from . import auth

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dashboard")

app = FastAPI(title="K8s Healing Agent Dashboard")

templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)


def require_auth(request: Request):
    if not auth.is_authenticated(request):
        return RedirectResponse(url="/login")
    return None


# Multi-cloud client cache
_cloud_clients = {}


def get_clients(cloud: str = "", region: str = ""):
    """Get K8s clients for the selected cloud/region. Returns None,None if no cluster found."""
    local_region = os.getenv("AWS_REGION", "ap-south-1")

    if not cloud or (cloud == "aws" and region == local_region):
        return core_v1, apps_v1

    cache_key = f"{cloud}/{region}"
    if cache_key in _cloud_clients:
        return _cloud_clients[cache_key]

    try:
        from .multi_cloud import MultiCloudManager
        mgr = MultiCloudManager()
        mgr.discover_all()
        for cluster in mgr.clusters.values():
            if cluster.cloud == cloud and cluster.region == region and cluster.core_v1:
                _cloud_clients[cache_key] = (cluster.core_v1, cluster.apps_v1)
                return cluster.core_v1, cluster.apps_v1
    except Exception as e:
        logger.error(f"Multi-cloud client error for {cloud}/{region}: {e}")

    # No cluster found in this cloud/region — return None
    return None, None

REPORT_NAMESPACE = os.getenv("REPORT_NAMESPACE", "atlas")
EXCLUDE_NAMESPACES = os.getenv("EXCLUDE_NAMESPACES", "kube-system,kube-public,kube-node-lease").split(",")

# ============================================
# Response Cache — reduces K8s API calls
# ============================================
import time as _time

_cache = {}
CACHE_TTL = 15  # seconds


def cached(key, ttl=CACHE_TTL):
    """Get cached value if fresh."""
    if key in _cache:
        val, ts = _cache[key]
        if _time.time() - ts < ttl:
            return val
    return None


def set_cache(key, value, ttl=CACHE_TTL):
    """Store value in cache."""
    _cache[key] = (value, _time.time())
    return value


def get_k8s_clients():
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
    return k8s_client.CoreV1Api(), k8s_client.AppsV1Api()


core_v1, apps_v1 = get_k8s_clients()


def classify_pod(pod):
    state = "Running"
    reason = ""
    message = ""
    container = ""
    restart_count = 0

    if pod.status.container_statuses:
        for cs in pod.status.container_statuses:
            restart_count = max(restart_count, cs.restart_count or 0)
            if cs.state and cs.state.waiting:
                r = cs.state.waiting.reason or ""
                state = r if r else "Waiting"
                reason = r
                message = cs.state.waiting.message or ""
                container = cs.name
                break
            if cs.state and cs.state.terminated:
                r = cs.state.terminated.reason or ""
                state = r if r else "Terminated"
                reason = r
                message = cs.state.terminated.message or ""
                container = cs.name
            if cs.last_state and cs.last_state.terminated:
                r = cs.last_state.terminated.reason or ""
                if r == "OOMKilled":
                    state = "OOMKilled"
                    reason = r
                    container = cs.name

    if state == "Running" and pod.status.phase == "Pending":
        state = "Pending"
        if pod.status.conditions:
            for cond in pod.status.conditions:
                if cond.type == "PodScheduled" and cond.status == "False":
                    reason = cond.reason or ""
                    message = cond.message or ""

    if state == "Running" and pod.status.container_statuses:
        all_ready = all(cs.ready for cs in pod.status.container_statuses)
        if not all_ready:
            state = "NotReady"

    is_healthy = state == "Running"
    severity = "healthy"
    if not is_healthy:
        if state in ("CrashLoopBackOff", "OOMKilled", "Error"):
            severity = "critical"
        elif state in ("Pending", "ImagePullBackOff", "ErrImagePull"):
            severity = "warning"
        else:
            severity = "warning"

    ready_str = "0/0"
    if pod.status.container_statuses:
        total = len(pod.status.container_statuses)
        ready = sum(1 for cs in pod.status.container_statuses if cs.ready)
        ready_str = f"{ready}/{total}"

    return {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "node": pod.spec.node_name or "Not assigned",
        "state": state,
        "reason": reason,
        "message": message[:200],
        "container": container,
        "restart_count": restart_count,
        "ready": ready_str,
        "is_healthy": is_healthy,
        "severity": severity,
        "age": _age(pod.metadata.creation_timestamp),
    }


def _age(ts):
    if not ts:
        return "Unknown"
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = now - ts
    days = delta.days
    hours = delta.seconds // 3600
    mins = (delta.seconds % 3600) // 60
    if days > 0:
        return f"{days}d{hours}h"
    if hours > 0:
        return f"{hours}h{mins}m"
    return f"{mins}m"


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, namespace: Optional[str] = None, cloud: Optional[str] = None, region: Optional[str] = None):
    redirect = require_auth(request)
    if redirect:
        return redirect
    user = auth.get_session(request) or {"email": "anonymous", "name": "Anonymous"}

    selected_cloud = cloud or ""
    selected_region = region or ""
    c_v1, a_v1 = get_clients(selected_cloud, selected_region)

    # No cluster found in this cloud/region
    no_cluster = c_v1 is None
    if no_cluster:
        cloud_names = {"aws": "AWS", "azure": "Azure", "gcp": "GCP"}
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "namespaces": [],
            "selected_namespace": "",
            "total": 0, "healthy": 0, "unhealthy": 0, "critical": 0, "warning": 0,
            "issues": [], "reports": [], "pods": [],
            "user": user,
            "no_cluster": True,
            "no_cluster_cloud": cloud_names.get(selected_cloud, selected_cloud),
            "no_cluster_region": selected_region,
        })

    cache_key = f"dashboard:{selected_cloud}:{selected_region}:{namespace}"
    cached_data = cached(cache_key)
    if cached_data:
        cached_data["request"] = request
        cached_data["user"] = user
        return templates.TemplateResponse("dashboard.html", cached_data)

    namespaces = [
        ns.metadata.name for ns in c_v1.list_namespace().items
        if ns.metadata.name not in EXCLUDE_NAMESPACES
    ]

    pods_data = []
    target_ns = [namespace] if namespace else namespaces

    for ns in target_ns:
        try:
            pods = c_v1.list_namespaced_pod(ns)
            for pod in pods.items:
                pods_data.append(classify_pod(pod))
        except Exception as e:
            logger.error(f"Error listing pods in {ns}: {e}")

    total = len(pods_data)
    healthy = sum(1 for p in pods_data if p["is_healthy"])
    unhealthy = total - healthy
    critical = sum(1 for p in pods_data if p["severity"] == "critical")
    warning = sum(1 for p in pods_data if p["severity"] == "warning")

    issues = [p for p in pods_data if not p["is_healthy"]]
    issues.sort(key=lambda x: (0 if x["severity"] == "critical" else 1, x["namespace"], x["name"]))

    reports = _get_reports(namespace)

    template_data = {
        "namespaces": namespaces,
        "selected_namespace": namespace or "",
        "total": total,
        "healthy": healthy,
        "unhealthy": unhealthy,
        "critical": critical,
        "warning": warning,
        "issues": issues,
        "reports": reports,
        "pods": pods_data,
        "no_cluster": False,
        "no_cluster_cloud": "",
        "no_cluster_region": "",
    }
    set_cache(cache_key, template_data)

    return templates.TemplateResponse("dashboard.html", {
        **template_data,
        "request": request,
        "user": user,
    })


@app.get("/api/pods")
async def api_pods(namespace: Optional[str] = None, cloud: Optional[str] = None, region: Optional[str] = None):
    cache_key = f"pods:{cloud}:{region}:{namespace}"
    cached_result = cached(cache_key)
    if cached_result is not None:
        return cached_result

    c_v1, _ = get_clients(cloud or "", region or "")
    if c_v1 is None:
        return []
    namespaces = [namespace] if namespace else [
        ns.metadata.name for ns in c_v1.list_namespace().items
        if ns.metadata.name not in EXCLUDE_NAMESPACES
    ]
    pods_data = []
    for ns in namespaces:
        try:
            pods = c_v1.list_namespaced_pod(ns)
            for pod in pods.items:
                pods_data.append(classify_pod(pod))
        except Exception:
            pass
    return set_cache(cache_key, pods_data)


@app.get("/api/reports")
async def api_reports():
    return _get_reports()


@app.get("/api/report/{report_id}")
async def api_report_detail(report_id: str):
    try:
        cm = core_v1.read_namespaced_config_map(f"healing-report-{report_id}", REPORT_NAMESPACE)
        return {"report_id": report_id, "content": cm.data.get("report.md", "")}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/resolve")
async def resolve_issue(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False, "message": "Unauthorized"}, status_code=401)
    body = await request.json()
    pod_name = body.get("pod_name")
    namespace = body.get("namespace")
    state = body.get("state", "")

    if not pod_name or not namespace:
        return JSONResponse({"success": False, "message": "Missing pod_name or namespace"}, status_code=400)

    try:
        # Step 1: Collect diagnostic data
        logs = _get_pod_logs(pod_name, namespace)
        events = _get_pod_events(pod_name, namespace)
        pod = core_v1.read_namespaced_pod(pod_name, namespace)
        pod_info = classify_pod(pod)

        # Get owner info
        owner_kind, owner_name = None, None
        if pod.metadata.owner_references:
            for ref in pod.metadata.owner_references:
                if ref.kind == "ReplicaSet":
                    try:
                        rs = apps_v1.read_namespaced_replica_set(ref.name, namespace)
                        if rs.metadata.owner_references:
                            for rs_ref in rs.metadata.owner_references:
                                if rs_ref.kind == "Deployment":
                                    owner_kind, owner_name = "Deployment", rs_ref.name
                    except Exception:
                        pass
                    if not owner_kind:
                        owner_kind, owner_name = "ReplicaSet", ref.name
                else:
                    owner_kind, owner_name = ref.kind, ref.name

        # Get resource info
        resources = {}
        if pod.spec.containers:
            c = pod.spec.containers[0]
            if c.resources and c.resources.limits:
                resources = dict(c.resources.limits)

        # Step 2: Call Bedrock AI for analysis
        bedrock_region = os.getenv("BEDROCK_REGION", "ap-south-1")
        bedrock_model = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
        bedrock_endpoint = os.getenv("BEDROCK_ENDPOINT_URL", "")

        bedrock_kwargs = {"service_name": "bedrock-runtime", "region_name": bedrock_region}
        if bedrock_endpoint:
            bedrock_kwargs["endpoint_url"] = bedrock_endpoint
        bedrock = boto3.client(**bedrock_kwargs)

        prompt = f"""You are a Kubernetes expert. Analyze this pod issue and recommend the best action.

Pod: {namespace}/{pod_name}
State: {state}
Owner: {owner_kind}/{owner_name}
Resources: {json.dumps(resources)}

Logs (last 50 lines):
{logs[:3000] if logs else 'No logs'}

Events:
{chr(10).join(events[:10]) if events else 'No events'}

Respond in JSON:
{{"root_cause": "<cause>", "recommended_action": "<one of: increase_memory, restart_pod, rollback_deployment, delete_and_recreate, no_action>", "confidence": <0.0-1.0>, "explanation": "<details>", "prevention_steps": ["<step1>", "<step2>"]}}"""

        ai_response = bedrock.invoke_model(
            modelId=bedrock_model,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            }),
            contentType="application/json",
            accept="application/json",
        )
        ai_text = json.loads(ai_response["body"].read())["content"][0]["text"]

        # Parse AI response
        import re
        json_match = re.search(r'\{[\s\S]*\}', ai_text)
        ai_data = json.loads(json_match.group()) if json_match else {}

        root_cause = ai_data.get("root_cause", "Unknown")
        action = ai_data.get("recommended_action", "no_action")
        confidence = float(ai_data.get("confidence", 0.5))
        explanation = ai_data.get("explanation", "")
        prevention = ai_data.get("prevention_steps", [])

        # Step 3: Execute remediation
        action_result = "No action taken"
        action_success = True

        if confidence < 0.7:
            action_result = f"Confidence too low ({confidence:.2f}). Report generated only."
            action = "no_action"
        elif action == "restart_pod":
            core_v1.delete_namespaced_pod(pod_name, namespace)
            action_result = f"Deleted pod {namespace}/{pod_name} for controller to recreate"
        elif action == "delete_and_recreate":
            core_v1.delete_namespaced_pod(pod_name, namespace)
            action_result = f"Deleted pod {namespace}/{pod_name} for controller to recreate"
        elif action == "increase_memory" and owner_kind == "Deployment" and owner_name:
            current_mem = resources.get("memory", "256Mi")
            current_mi = _parse_memory(current_mem)
            new_mi = min(int(current_mi * 1.5), 4096)
            if new_mi > current_mi:
                container_name = pod.spec.containers[0].name
                patch = {"spec": {"template": {"spec": {"containers": [{"name": container_name, "resources": {"limits": {"memory": f"{new_mi}Mi"}}}]}}}}
                apps_v1.patch_namespaced_deployment(owner_name, namespace, patch)
                action_result = f"Increased memory from {current_mi}Mi to {new_mi}Mi on {owner_name}"
            else:
                action_result = f"Memory already at max cap ({current_mi}Mi)"
        elif action == "rollback_deployment" and owner_kind == "Deployment" and owner_name:
            core_v1.delete_namespaced_pod(pod_name, namespace)
            action_result = f"Restarted pod {pod_name} (rollback requires manual review)"
        else:
            action_result = f"Action '{action}' requires manual intervention"

        # Step 4: Store report
        import uuid
        report_id = str(uuid.uuid4())[:8]
        report_content = f"""## Self-Healing Report: {report_id}
- **Timestamp**: {datetime.now(timezone.utc).isoformat()}Z
- **Pod**: {namespace}/{pod_name}
- **State Detected**: {state}
- **Triggered By**: Dashboard Manual Resolve

### Root Cause
{root_cause}

### AI Analysis
{explanation}

### Action Taken
- **Action**: {action}
- **Success**: {action_success}
- **Confidence**: {confidence:.2f}
- **Details**: {action_result}

### Prevention Steps
""" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(prevention))

        cm = k8s_client.V1ConfigMap(
            metadata=k8s_client.V1ObjectMeta(
                name=f"healing-report-{report_id}",
                namespace=REPORT_NAMESPACE,
                labels={"app": "k8s-healing-agent", "type": "healing-report"},
            ),
            data={"report.md": report_content},
        )
        core_v1.create_namespaced_config_map(REPORT_NAMESPACE, cm)

        return {
            "success": True,
            "report_id": report_id,
            "root_cause": root_cause,
            "action": action,
            "action_result": action_result,
            "confidence": confidence,
            "prevention_steps": prevention,
        }

    except Exception as e:
        logger.error(f"Resolve failed for {namespace}/{pod_name}: {e}")
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


def _get_pod_logs(pod_name, namespace):
    try:
        return core_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=50)
    except Exception:
        try:
            return core_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=50, previous=True)
        except Exception:
            return None


def _get_pod_events(pod_name, namespace):
    try:
        events = core_v1.list_namespaced_event(namespace, field_selector=f"involvedObject.name={pod_name}")
        return [f"[{e.type}] {e.reason}: {e.message}" for e in events.items[-10:]]
    except Exception:
        return []


def _parse_memory(value):
    value = str(value)
    if value.endswith("Gi"):
        return int(float(value[:-2]) * 1024)
    if value.endswith("Mi"):
        return int(float(value[:-2]))
    return 256


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.is_authenticated(request):
        return RedirectResponse(url="/")
    if not auth.AUTH_ENABLED:
        return RedirectResponse(url="/")
    login_url = auth.get_login_url()
    return templates.TemplateResponse("login.html", {"request": request, "login_url": login_url})


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = ""):
    if not code:
        return RedirectResponse(url="/login")
    try:
        tokens = await auth.exchange_code(code)
    except Exception:
        return RedirectResponse(url="/login")
    id_token = tokens.get("id_token", "")
    access_token = tokens.get("access_token", "")
    claims = await auth.verify_token(id_token, access_token)
    email = claims.get("email", "")
    name = claims.get("name", email)
    session_token = auth.create_session(email, name)
    response = RedirectResponse(url="/")
    response.set_cookie("session", session_token, httponly=True, secure=True, samesite="lax", max_age=86400)
    logger.info(f"User logged in: {email}")
    return response


@app.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/login")
    response.delete_cookie("session")
    return response


@app.get("/auth/logout")
async def cognito_logout():
    return RedirectResponse(url=auth.get_logout_url())


@app.get("/gpu", response_class=HTMLResponse)
async def gpu_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    user = auth.get_session(request) or {"email": "anonymous", "name": "Anonymous"}
    return templates.TemplateResponse("gpu.html", {"request": request, "user": user})


@app.post("/api/gpu-analyze")
async def api_gpu(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)
    try:
        from .gpu_optimizer import GPUOptimizer
        optimizer = GPUOptimizer(core_v1)
        return {"success": True, **optimizer.analyze()}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/binpack", response_class=HTMLResponse)
async def binpack_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    user = auth.get_session(request) or {"email": "anonymous", "name": "Anonymous"}
    return templates.TemplateResponse("binpack.html", {"request": request, "user": user})


@app.get("/security", response_class=HTMLResponse)
async def security_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    user = auth.get_session(request) or {"email": "anonymous", "name": "Anonymous"}
    return templates.TemplateResponse("security.html", {"request": request, "user": user})


@app.get("/predict", response_class=HTMLResponse)
async def predict_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    user = auth.get_session(request) or {"email": "anonymous", "name": "Anonymous"}
    return templates.TemplateResponse("predict.html", {"request": request, "user": user})


@app.post("/api/binpack")
async def api_binpack(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)
    try:
        from .bin_packer import BinPacker
        bp = BinPacker(core_v1, apps_v1)
        return {"success": True, **bp.analyze()}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/security-scan")
async def api_security(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)
    try:
        from .security_scanner import SecurityScanner
        scanner = SecurityScanner(core_v1)
        return {"success": True, **scanner.full_scan()}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/predict")
async def api_predict(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)
    try:
        from .prediction_engine import PredictionEngine
        pe = PredictionEngine(core_v1, apps_v1)
        return {"success": True, **pe.analyze()}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/spot", response_class=HTMLResponse)
async def spot_dashboard(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    user = auth.get_session(request) or {"email": "anonymous", "name": "Anonymous"}
    return templates.TemplateResponse("spot.html", {"request": request, "user": user})


@app.get("/api/spot-status")
async def spot_status(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)
    cached_result = cached("spot-status", ttl=30)
    if cached_result:
        return cached_result
    try:
        from .spot_manager import SpotManager
        sm = SpotManager(core_v1, apps_v1)
        savings = sm.get_spot_savings()
        az = sm.get_az_distribution()

        # Get interruption reports
        interruptions = []
        try:
            cms = core_v1.list_namespaced_config_map(
                REPORT_NAMESPACE, label_selector="app=k8s-healing-agent,type=spot-interruption"
            )
            for cm in sorted(cms.items, key=lambda x: x.metadata.creation_timestamp, reverse=True)[:20]:
                interruptions.append({"content": cm.data.get("report.md", ""), "age": _age(cm.metadata.creation_timestamp)})
        except Exception:
            pass

        return {"success": True, "savings": savings, "az_distribution": az, "interruptions": interruptions}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/costs", response_class=HTMLResponse)
async def costs_dashboard(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    user = auth.get_session(request) or {"email": "anonymous", "name": "Anonymous"}
    return templates.TemplateResponse("costs.html", {"request": request, "user": user})


@app.get("/api/cost-breakdown")
async def cost_breakdown(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)
    cached_result = cached("cost-breakdown", ttl=30)
    if cached_result:
        return cached_result
    try:
        from .cost_attribution import CostAttribution
        ca = CostAttribution(core_v1, apps_v1)
        data = ca.get_full_breakdown()
        result = {"success": True, **data}
        return set_cache("cost-breakdown", result, ttl=30)
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/cost-trend")
async def cost_trend(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)
    try:
        from .cost_attribution import CostAttribution
        ca = CostAttribution(core_v1, apps_v1)
        trend = ca.get_cost_trend(days=30)
        return {"success": True, "trend": trend}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/cost-snapshot")
async def save_snapshot(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)
    try:
        from .cost_attribution import CostAttribution
        ca = CostAttribution(core_v1, apps_v1)
        ca.store_daily_snapshot()
        return {"success": True, "message": "Snapshot saved"}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/optimizer", response_class=HTMLResponse)
async def optimizer_dashboard(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    user = auth.get_session(request) or {"email": "anonymous", "name": "Anonymous"}

    # Get optimization reports
    opt_reports = []
    try:
        cms = core_v1.list_namespaced_config_map(
            REPORT_NAMESPACE,
            label_selector="app=k8s-healing-agent,type=optimization-report",
        )
        for cm in sorted(cms.items, key=lambda x: x.metadata.creation_timestamp, reverse=True)[:20]:
            opt_reports.append({
                "id": cm.metadata.name,
                "content": cm.data.get("report.md", ""),
                "age": _age(cm.metadata.creation_timestamp),
            })
    except Exception:
        pass

    # Get node optimization reports
    node_reports = []
    try:
        cms = core_v1.list_namespaced_config_map(
            REPORT_NAMESPACE,
            label_selector="app=k8s-healing-agent,type=node-optimization",
        )
        for cm in sorted(cms.items, key=lambda x: x.metadata.creation_timestamp, reverse=True)[:10]:
            node_reports.append({
                "id": cm.metadata.name,
                "content": cm.data.get("report.json", ""),
                "age": _age(cm.metadata.creation_timestamp),
            })
    except Exception:
        pass

    # Get agent pod logs (last 50 lines with optimizer entries)
    agent_logs = []
    try:
        pods = core_v1.list_namespaced_pod(
            "atlas-dev", label_selector="app=k8s-healing-agent"
        )
        if pods.items:
            logs = core_v1.read_namespaced_pod_log(
                pods.items[0].metadata.name, "atlas-dev", tail_lines=200
            )
            for line in logs.split("\n"):
                if "optim" in line.lower() or "autoscale" in line.lower() or "rightsize" in line.lower() or "consolidat" in line.lower():
                    agent_logs.append(line)
            agent_logs = agent_logs[-50:]
    except Exception:
        pass

    return templates.TemplateResponse("optimizer.html", {
        "request": request,
        "user": user,
        "opt_reports": opt_reports,
        "node_reports": node_reports,
        "agent_logs": agent_logs,
    })


@app.get("/nodes", response_class=HTMLResponse)
async def nodes_dashboard(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    user = auth.get_session(request) or {"email": "anonymous", "name": "Anonymous"}
    return templates.TemplateResponse("nodes.html", {"request": request, "user": user})


@app.get("/cost", response_class=HTMLResponse)
async def cost_dashboard(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    user = auth.get_session(request) or {"email": "anonymous", "name": "Anonymous"}

    # Get cost reports from ConfigMaps
    cost_reports = []
    try:
        cms = core_v1.list_namespaced_config_map(
            REPORT_NAMESPACE,
            label_selector="app=k8s-healing-agent,type=cost-report",
        )
        for cm in sorted(cms.items, key=lambda x: x.metadata.creation_timestamp, reverse=True)[:10]:
            cost_reports.append({
                "id": cm.metadata.name,
                "content": cm.data.get("report.md", ""),
                "age": _age(cm.metadata.creation_timestamp),
            })
    except Exception:
        pass

    return templates.TemplateResponse("cost.html", {
        "request": request,
        "user": user,
        "cost_reports": cost_reports,
    })


@app.post("/api/cost/analyze")
async def run_cost_analysis(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False, "message": "Unauthorized"}, status_code=401)

    try:
        from .cost_optimizer import CostOptimizer
        optimizer = CostOptimizer(core_v1, apps_v1)
        report = optimizer.analyze()
        content = optimizer.format_report(report)

        # Store as ConfigMap
        cm = k8s_client.V1ConfigMap(
            metadata=k8s_client.V1ObjectMeta(
                name=f"cost-report-{report.report_id}",
                namespace=REPORT_NAMESPACE,
                labels={"app": "k8s-healing-agent", "type": "cost-report"},
            ),
            data={"report.md": content},
        )
        core_v1.create_namespaced_config_map(REPORT_NAMESPACE, cm)

        return {
            "success": True,
            "report_id": report.report_id,
            "monthly_cost": report.estimated_monthly_cost,
            "potential_savings": report.potential_savings,
            "oversized_pods": len(report.oversized_pods),
            "idle_pods": len(report.idle_pods),
            "node_recommendations": len(report.node_recommendations),
            "spot_recommendations": len(report.spot_recommendations),
            "ai_summary": report.ai_summary,
            "content": content,
        }
    except Exception as e:
        logger.error(f"Cost analysis failed: {e}")
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/clusters")
async def list_clusters(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)
    try:
        from .multi_cluster import MultiClusterManager
        mgr = MultiClusterManager()
        clusters = mgr.discover_clusters()
        return {"clusters": mgr.list_clusters()}
    except Exception as e:
        return {"clusters": [], "error": str(e)}


@app.post("/api/node-optimize")
async def node_optimize(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False, "message": "Unauthorized"}, status_code=401)
    try:
        from .node_optimizer import NodeOptimizer
        optimizer = NodeOptimizer(core_v1)
        result = optimizer.analyze_and_recommend()
        return {"success": True, **result}
    except Exception as e:
        logger.error(f"Node optimization failed: {e}")
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/node-replace")
async def node_replace(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False, "message": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
        node_group = body.get("node_group")
        new_type = body.get("new_instance_type")
        new_capacity = body.get("new_capacity_type", "SPOT")

        from .node_optimizer import NodeOptimizer
        optimizer = NodeOptimizer(core_v1)
        result = optimizer.execute_replacement(node_group, new_type, new_capacity)
        return result
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/multi-cloud")
async def api_multi_cloud(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)

    cached_result = cached("multi-cloud", ttl=60)
    if cached_result:
        return cached_result

    try:
        from .multi_cloud import MultiCloudManager
        mgr = MultiCloudManager()
        mgr.discover_all()
        result = {"success": True, "clusters": mgr.list_all()}
        set_cache("multi-cloud", result, ttl=60)
        return result
    except Exception as e:
        return {"success": True, "clusters": [], "error": str(e)}


@app.get("/api/sla")
async def api_sla(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)
    try:
        from .enterprise import SLAMonitor
        sla = SLAMonitor(core_v1)
        return {"success": True, **sla.check()}
    except Exception as e:
        return {"success": True, "error": str(e)}


@app.post("/api/test-notification")
async def test_notification(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)
    try:
        from .enterprise import NotificationManager
        nm = NotificationManager()
        nm.notify("🧪 Test Notification", "This is a test from K8s Healing Agent dashboard.", "info")
        return {"success": True, "message": "Notification sent to all configured channels"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/api/action-log")
async def action_log(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)
    try:
        from .persistent_store import PersistentStore
        store = PersistentStore()
        actions = store.get_action_log(limit=50)
        return {"success": True, "actions": actions}
    except Exception as e:
        return {"success": True, "actions": [], "note": f"DynamoDB unavailable: {e}"}


@app.get("/api/usage-history/{deployment_key}")
async def usage_history(request: Request, deployment_key: str):
    if not auth.is_authenticated(request):
        return JSONResponse({"success": False}, status_code=401)
    try:
        from .persistent_store import PersistentStore
        store = PersistentStore()
        history = store.get_usage_history(deployment_key, limit=30)
        return {"success": True, "history": history}
    except Exception as e:
        return {"success": True, "history": [], "note": f"DynamoDB unavailable: {e}"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


def _get_reports(filter_namespace=None):
    reports = []
    try:
        cms = core_v1.list_namespaced_config_map(
            REPORT_NAMESPACE,
            label_selector="app=k8s-healing-agent,type=healing-report",
        )
        for cm in sorted(cms.items, key=lambda x: x.metadata.creation_timestamp, reverse=True)[:50]:
            content = cm.data.get("report.md", "")
            pod_line = ""
            state_line = ""
            root_cause = ""
            action = ""
            success = ""
            for line in content.split("\n"):
                if "**Pod**:" in line:
                    pod_line = line.split("**Pod**:")[-1].strip()
                elif "**State Detected**:" in line:
                    state_line = line.split("**State Detected**:")[-1].strip()
                elif "### Root Cause" in line:
                    idx = content.index("### Root Cause")
                    root_cause = content[idx:].split("\n")[1].strip()
                elif "**Action**:" in line:
                    action = line.split("**Action**:")[-1].strip()
                elif "**Success**:" in line:
                    success = line.split("**Success**:")[-1].strip()

            # Filter by namespace if specified
            if filter_namespace and filter_namespace not in pod_line:
                continue

            reports.append({
                "id": cm.metadata.name.replace("healing-report-", ""),
                "pod": pod_line,
                "state": state_line,
                "root_cause": root_cause[:150],
                "action": action,
                "success": success,
                "age": _age(cm.metadata.creation_timestamp),
                "content": content,
            })
    except Exception as e:
        logger.error(f"Error fetching reports: {e}")
    return reports
