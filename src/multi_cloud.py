"""
Multi-Cloud Support
- AWS EKS
- Azure AKS
- GCP GKE
- Single control plane for all clusters
- Cloud-specific cost optimization
"""

import os
import json
import logging
import base64
import tempfile
from dataclasses import dataclass
from typing import Optional

from kubernetes import client as k8s_client, config as k8s_config

from . import config

logger = logging.getLogger("multi-cloud")


@dataclass
class CloudCluster:
    name: str
    cloud: str  # aws, azure, gcp
    region: str
    endpoint: str
    version: str
    status: str
    node_count: int
    pod_count: int
    unhealthy_pods: int
    monthly_cost_estimate: float
    core_v1: Optional[k8s_client.CoreV1Api] = None
    apps_v1: Optional[k8s_client.AppsV1Api] = None


class MultiCloudManager:
    def __init__(self):
        self.clusters = {}
        self.aws_regions = [r.strip() for r in os.getenv("AWS_REGIONS", "ap-south-1").split(",") if r.strip()]
        self.azure_enabled = os.getenv("AZURE_ENABLED", "false").lower() == "true"
        self.gcp_enabled = os.getenv("GCP_ENABLED", "false").lower() == "true"

    def discover_all(self) -> list:
        """Discover clusters across all configured clouds."""
        all_clusters = []

        # AWS EKS
        all_clusters.extend(self._discover_aws())

        # Azure AKS
        if self.azure_enabled:
            all_clusters.extend(self._discover_azure())

        # GCP GKE
        if self.gcp_enabled:
            all_clusters.extend(self._discover_gcp())

        for c in all_clusters:
            self.clusters[f"{c.cloud}/{c.region}/{c.name}"] = c

        return all_clusters

    # =====================================================
    # AWS EKS
    # =====================================================
    def _discover_aws(self) -> list:
        clusters = []
        try:
            import boto3
            for region in self.aws_regions:
                try:
                    eks = boto3.client("eks", region_name=region)
                    names = eks.list_clusters().get("clusters", [])
                    for name in names:
                        try:
                            info = eks.describe_cluster(name=name)["cluster"]
                            cluster = CloudCluster(
                                name=name, cloud="aws", region=region,
                                endpoint=info["endpoint"], version=info["version"],
                                status=info["status"], node_count=0, pod_count=0,
                                unhealthy_pods=0, monthly_cost_estimate=0,
                            )

                            # Try connecting
                            try:
                                core_v1, apps_v1 = self._connect_aws(eks, name, region, info)
                                cluster.core_v1 = core_v1
                                cluster.apps_v1 = apps_v1
                                self._enrich_cluster(cluster)
                            except Exception as e:
                                logger.debug(f"Cannot connect to AWS {name}: {e}")

                            clusters.append(cluster)
                        except Exception as e:
                            logger.error(f"Error describing AWS cluster {name}: {e}")
                except Exception as e:
                    logger.error(f"Error listing AWS clusters in {region}: {e}")
        except ImportError:
            logger.warning("boto3 not available for AWS")
        return clusters

    def _connect_aws(self, eks_client, cluster_name, region, cluster_info):
        import boto3
        from botocore.signers import RequestSigner

        sts = boto3.client("sts", region_name=region)
        service_id = sts.meta.service_model.service_id
        signer = RequestSigner(service_id, region, "sts", "v4",
                               sts._request_signer._credentials, sts.meta.events)
        params = {
            "method": "GET",
            "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
            "body": {}, "headers": {"x-k8s-aws-id": cluster_name}, "context": {},
        }
        signed_url = signer.generate_presigned_url(params, region_name=region, expires_in=60, operation_name="")
        token = "k8s-aws-v1." + base64.urlsafe_b64encode(signed_url.encode()).decode().rstrip("=")

        ca_data = cluster_info["certificateAuthority"]["data"]
        ca_path = tempfile.mktemp(suffix=".crt")
        with open(ca_path, "w") as f:
            f.write(base64.b64decode(ca_data).decode())

        configuration = k8s_client.Configuration()
        configuration.host = cluster_info["endpoint"]
        configuration.api_key["authorization"] = f"Bearer {token}"
        configuration.ssl_ca_cert = ca_path
        api = k8s_client.ApiClient(configuration)
        return k8s_client.CoreV1Api(api), k8s_client.AppsV1Api(api)

    # =====================================================
    # Azure AKS
    # =====================================================
    def _discover_azure(self) -> list:
        clusters = []
        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.containerservice import ContainerServiceClient

            subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID", "")
            if not subscription_id:
                logger.warning("AZURE_SUBSCRIPTION_ID not set")
                return clusters

            credential = DefaultAzureCredential()
            aks_client = ContainerServiceClient(credential, subscription_id)

            for cluster in aks_client.managed_clusters.list():
                try:
                    c = CloudCluster(
                        name=cluster.name, cloud="azure",
                        region=cluster.location,
                        endpoint=cluster.fqdn or "",
                        version=cluster.kubernetes_version or "",
                        status=cluster.provisioning_state or "",
                        node_count=0, pod_count=0, unhealthy_pods=0,
                        monthly_cost_estimate=0,
                    )

                    # Try connecting
                    try:
                        rg = cluster.id.split("/resourceGroups/")[1].split("/")[0]
                        creds = aks_client.managed_clusters.list_cluster_admin_credentials(rg, cluster.name)
                        kubeconfig = base64.b64decode(creds.kubeconfigs[0].value).decode()
                        core_v1, apps_v1 = self._connect_kubeconfig(kubeconfig)
                        c.core_v1 = core_v1
                        c.apps_v1 = apps_v1
                        self._enrich_cluster(c)
                    except Exception as e:
                        logger.debug(f"Cannot connect to Azure {cluster.name}: {e}")

                    clusters.append(c)
                except Exception as e:
                    logger.error(f"Error processing Azure cluster {cluster.name}: {e}")

        except ImportError:
            logger.info("Azure SDK not installed. Install: pip install azure-identity azure-mgmt-containerservice")
        except Exception as e:
            logger.error(f"Azure discovery error: {e}")
        return clusters

    # =====================================================
    # GCP GKE
    # =====================================================
    def _discover_gcp(self) -> list:
        clusters = []
        try:
            from google.cloud import container_v1
            from google.auth import default as google_default
            from google.auth.transport.requests import Request

            project_id = os.getenv("GCP_PROJECT_ID", "")
            if not project_id:
                logger.warning("GCP_PROJECT_ID not set")
                return clusters

            gke_client = container_v1.ClusterManagerClient()
            parent = f"projects/{project_id}/locations/-"

            for cluster in gke_client.list_clusters(parent=parent).clusters:
                try:
                    c = CloudCluster(
                        name=cluster.name, cloud="gcp",
                        region=cluster.location,
                        endpoint=cluster.endpoint or "",
                        version=cluster.current_master_version or "",
                        status=cluster.status.name if cluster.status else "",
                        node_count=cluster.current_node_count or 0,
                        pod_count=0, unhealthy_pods=0,
                        monthly_cost_estimate=0,
                    )

                    # Try connecting
                    try:
                        credentials, _ = google_default()
                        credentials.refresh(Request())
                        token = credentials.token

                        ca_path = tempfile.mktemp(suffix=".crt")
                        with open(ca_path, "wb") as f:
                            f.write(base64.b64decode(cluster.master_auth.cluster_ca_certificate))

                        configuration = k8s_client.Configuration()
                        configuration.host = f"https://{cluster.endpoint}"
                        configuration.api_key["authorization"] = f"Bearer {token}"
                        configuration.ssl_ca_cert = ca_path
                        api = k8s_client.ApiClient(configuration)
                        c.core_v1 = k8s_client.CoreV1Api(api)
                        c.apps_v1 = k8s_client.AppsV1Api(api)
                        self._enrich_cluster(c)
                    except Exception as e:
                        logger.debug(f"Cannot connect to GCP {cluster.name}: {e}")

                    clusters.append(c)
                except Exception as e:
                    logger.error(f"Error processing GCP cluster {cluster.name}: {e}")

        except ImportError:
            logger.info("GCP SDK not installed. Install: pip install google-cloud-container google-auth")
        except Exception as e:
            logger.error(f"GCP discovery error: {e}")
        return clusters

    # =====================================================
    # Helpers
    # =====================================================
    def _connect_kubeconfig(self, kubeconfig_str):
        import yaml
        kc = yaml.safe_load(kubeconfig_str)
        cluster = kc["clusters"][0]["cluster"]
        user = kc["users"][0]["user"]

        configuration = k8s_client.Configuration()
        configuration.host = cluster["server"]

        if "certificate-authority-data" in cluster:
            ca_path = tempfile.mktemp(suffix=".crt")
            with open(ca_path, "wb") as f:
                f.write(base64.b64decode(cluster["certificate-authority-data"]))
            configuration.ssl_ca_cert = ca_path

        if "token" in user:
            configuration.api_key["authorization"] = f"Bearer {user['token']}"
        elif "client-certificate-data" in user:
            cert_path = tempfile.mktemp(suffix=".crt")
            key_path = tempfile.mktemp(suffix=".key")
            with open(cert_path, "wb") as f:
                f.write(base64.b64decode(user["client-certificate-data"]))
            with open(key_path, "wb") as f:
                f.write(base64.b64decode(user["client-key-data"]))
            configuration.cert_file = cert_path
            configuration.key_file = key_path

        api = k8s_client.ApiClient(configuration)
        return k8s_client.CoreV1Api(api), k8s_client.AppsV1Api(api)

    def _enrich_cluster(self, cluster: CloudCluster):
        """Add node/pod counts and cost estimate."""
        if not cluster.core_v1:
            return
        try:
            nodes = cluster.core_v1.list_node()
            cluster.node_count = len(nodes.items)

            pods = cluster.core_v1.list_pod_for_all_namespaces()
            cluster.pod_count = len(pods.items)
            cluster.unhealthy_pods = sum(
                1 for p in pods.items
                if p.status.phase != "Running" or (
                    p.status.container_statuses and
                    not all(cs.ready for cs in p.status.container_statuses)
                )
            )

            # Rough cost estimate per node
            cost_per_node = {"aws": 22, "azure": 30, "gcp": 25}
            cluster.monthly_cost_estimate = cluster.node_count * cost_per_node.get(cluster.cloud, 25)
        except Exception as e:
            logger.debug(f"Error enriching {cluster.name}: {e}")

    def list_all(self) -> list:
        return [
            {
                "key": f"{c.cloud}/{c.region}/{c.name}",
                "name": c.name,
                "cloud": c.cloud,
                "cloud_icon": {"aws": "🟠", "azure": "🔵", "gcp": "🔴"}.get(c.cloud, "⚪"),
                "region": c.region,
                "version": c.version,
                "status": c.status,
                "nodes": c.node_count,
                "pods": c.pod_count,
                "unhealthy": c.unhealthy_pods,
                "cost": f"${c.monthly_cost_estimate:.0f}/mo",
                "connected": c.core_v1 is not None,
            }
            for c in self.clusters.values()
        ]
