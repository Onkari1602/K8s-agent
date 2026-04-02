import json
import logging
import re

import boto3

from . import config
from .models import AnalysisResult, PodIssue, RemediationAction

logger = logging.getLogger(__name__)


class BedrockAnalyzer:
    def __init__(self):
        self.client = self._create_client()
        self.model_id = config.BEDROCK_MODEL_ID
        self.max_tokens = config.BEDROCK_MAX_TOKENS

    def _create_client(self):
        kwargs = {
            "service_name": "bedrock-runtime",
            "region_name": config.BEDROCK_REGION,
        }
        if config.BEDROCK_ENDPOINT_URL:
            kwargs["endpoint_url"] = config.BEDROCK_ENDPOINT_URL
        return boto3.client(**kwargs)

    def analyze_issue(self, issue: PodIssue) -> AnalysisResult:
        prompt = self._build_prompt(issue)
        try:
            response = self._invoke_model(prompt)
            return self._parse_response(issue, response)
        except Exception as e:
            logger.error(f"Bedrock analysis failed: {e}")
            return AnalysisResult(
                issue=issue,
                root_cause=f"AI analysis failed: {e}",
                recommended_action=RemediationAction.NO_ACTION,
                confidence=0.0,
                explanation=str(e),
                prevention_steps=[],
                raw_ai_response="",
            )

    def _build_prompt(self, issue: PodIssue) -> str:
        events_text = "\n".join(issue.events[:20]) if issue.events else "No events"
        logs_text = issue.logs_tail[:3000] if issue.logs_tail else "No logs available"

        return f"""You are a Kubernetes cluster operations expert. Analyze the following pod issue and provide a structured diagnosis.

## Pod Issue Details
- Pod: {issue.namespace}/{issue.pod_name}
- Node: {issue.node_name or 'Not assigned'}
- State: {issue.state.value}
- Reason: {issue.reason}
- Message: {issue.message}
- Container: {issue.container_name or 'N/A'}
- Restart Count: {issue.restart_count}
- Owner: {issue.owner_kind or 'N/A'}/{issue.owner_name or 'N/A'}
- Resource Requests: {json.dumps(issue.resource_requests)}
- Resource Limits: {json.dumps(issue.resource_limits)}

## Recent Logs (last 50 lines):
{logs_text}

## Recent Events:
{events_text}

## Instructions
Analyze the root cause and recommend ONE action. Respond ONLY in this exact JSON format:
{{
  "root_cause": "<concise root cause in 1-2 sentences>",
  "recommended_action": "<one of: increase_memory, restart_pod, rollback_deployment, fix_image_reference, scale_node_group, delete_and_recreate, no_action>",
  "confidence": <float 0.0 to 1.0>,
  "explanation": "<detailed explanation of why this action is recommended>",
  "prevention_steps": ["<step1>", "<step2>", "<step3>"]
}}"""

    def _invoke_model(self, prompt: str) -> str:
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        })

        response = self.client.invoke_model(
            modelId=self.model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )

        result = json.loads(response["body"].read())
        return result["content"][0]["text"]

    def _parse_response(self, issue: PodIssue, response: str) -> AnalysisResult:
        try:
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                data = json.loads(json_match.group())
            else:
                raise ValueError("No JSON found in response")

            action_str = data.get("recommended_action", "no_action")
            try:
                action = RemediationAction(action_str)
            except ValueError:
                action = RemediationAction.NO_ACTION

            return AnalysisResult(
                issue=issue,
                root_cause=data.get("root_cause", "Unknown"),
                recommended_action=action,
                confidence=float(data.get("confidence", 0.5)),
                explanation=data.get("explanation", ""),
                prevention_steps=data.get("prevention_steps", []),
                raw_ai_response=response,
            )
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Failed to parse AI response: {e}")
            return AnalysisResult(
                issue=issue,
                root_cause="Failed to parse AI analysis",
                recommended_action=RemediationAction.NO_ACTION,
                confidence=0.0,
                explanation=response[:500],
                prevention_steps=[],
                raw_ai_response=response,
            )
