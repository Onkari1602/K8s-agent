import os
import json
import logging
from dataclasses import dataclass
from typing import Optional

import boto3
from kubernetes import client as k8s_client, config as k8s_config

logger = logging.getLogger(__name__)


@dataclass
class ClusterInfo:
    name: str
    region: str
    endpoint: str
    status: str
    version: str
    node_count: int
    pod_count: int
    unhealthy_pods: int
    core_v1: Optional[k8s_client.CoreV1Api] = None
    apps_v1: Optional[k8s_client.AppsV1Api] = None


class MultiClusterManager:
    def __init__(self):
        self.clusters = {}
        self.eks_clients = {}
        self.regions = [r.strip() for r in os.getenv("CLUSTER_REGIONS", "ap-south-1").split(",")]

    def discover_clusters(self) -> list[ClusterInfo]:
        """Discover all EKS clusters across configured regions."""
        all_clusters = []
        for region in self.regions:
            try:
                eks = self._get_eks_client(region)
                cluster_names = eks.list_clusters()["clusters"]
                for name in cluster_names:
                    info = self._get_cluster_info(eks, name, region)
                    if info:
                        all_clusters.append(info)
                        self.clusters[f"{region}/{name}"] = info
            except Exception as e:
                logger.error(f"Error discovering clusters in {region}: {e}")
        return all_clusters

    def _get_eks_client(self, region: str):
        if region not in self.eks_clients:
            self.eks_clients[region] = boto3.client("eks", region_name=region)
        return self.eks_clients[region]

    def _get_cluster_info(self, eks_client, cluster_name: str, region: str) -> Optional[ClusterInfo]:
        try:
            resp = eks_client.describe_cluster(name=cluster_name)
            cluster = resp["cluster"]

            info = ClusterInfo(
                name=cluster_name,
                region=region,
                endpoint=cluster["endpoint"],
                status=cluster["status"],
                version=cluster["version"],
                node_count=0,
                pod_count=0,
                unhealthy_pods=0,
            )

            # Try to get K8s clients for this cluster
            try:
                core_v1, apps_v1 = self._get_k8s_clients(cluster_name, region)
                info.core_v1 = core_v1
                info.apps_v1 = apps_v1

                # Count nodes
                nodes = core_v1.list_node()
                info.node_count = len(nodes.items)

                # Count pods across all namespaces
                pods = core_v1.list_pod_for_all_namespaces()
                info.pod_count = len(pods.items)
                info.unhealthy_pods = sum(
                    1 for p in pods.items
                    if p.status.phase != "Running" or (
                        p.status.container_statuses and
                        not all(cs.ready for cs in p.status.container_statuses)
                    )
                )
            except Exception as e:
                logger.warning(f"Cannot connect to cluster {cluster_name}: {e}")

            return info
        except Exception as e:
            logger.error(f"Error getting cluster info for {cluster_name}: {e}")
            return None

    def _get_k8s_clients(self, cluster_name: str, region: str):
        """Generate kubeconfig for a remote cluster and return K8s clients."""
        eks = self._get_eks_client(region)
        cluster = eks.describe_cluster(name=cluster_name)["cluster"]

        # Get token using STS
        sts = boto3.client("sts", region_name=region)
        token = self._get_bearer_token(cluster_name, sts)

        configuration = k8s_client.Configuration()
        configuration.host = cluster["endpoint"]
        configuration.api_key["authorization"] = f"Bearer {token}"
        configuration.ssl_ca_cert = self._write_ca_cert(cluster_name, cluster["certificateAuthority"]["data"])
        configuration.verify_ssl = True

        api_client = k8s_client.ApiClient(configuration)
        return k8s_client.CoreV1Api(api_client), k8s_client.AppsV1Api(api_client)

    def _get_bearer_token(self, cluster_name: str, sts_client) -> str:
        """Get EKS bearer token using STS presigned URL."""
        import base64
        from botocore.signers import RequestSigner

        service_id = sts_client.meta.service_model.service_id
        signer = RequestSigner(service_id, sts_client.meta.region_name, "sts",
                               "v4", sts_client._request_signer._credentials, sts_client.meta.events)

        params = {
            "method": "GET",
            "url": f"https://sts.{sts_client.meta.region_name}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
            "body": {},
            "headers": {"x-k8s-aws-id": cluster_name},
            "context": {},
        }

        signed_url = signer.generate_presigned_url(
            params, region_name=sts_client.meta.region_name, expires_in=60,
            operation_name=""
        )

        token = "k8s-aws-v1." + base64.urlsafe_b64encode(signed_url.encode("utf-8")).decode("utf-8").rstrip("=")
        return token

    def _write_ca_cert(self, cluster_name: str, ca_data: str) -> str:
        import base64
        import tempfile
        cert_path = f"/tmp/ca-{cluster_name}.crt"
        with open(cert_path, "w") as f:
            f.write(base64.b64decode(ca_data).decode("utf-8"))
        return cert_path

    def get_cluster(self, cluster_key: str) -> Optional[ClusterInfo]:
        return self.clusters.get(cluster_key)

    def list_clusters(self) -> list[dict]:
        return [
            {
                "key": f"{c.region}/{c.name}",
                "name": c.name,
                "region": c.region,
                "status": c.status,
                "version": c.version,
                "nodes": c.node_count,
                "pods": c.pod_count,
                "unhealthy": c.unhealthy_pods,
            }
            for c in self.clusters.values()
        ]
