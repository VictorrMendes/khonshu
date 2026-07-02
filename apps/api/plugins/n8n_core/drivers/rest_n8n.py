import httpx
import os
from datetime import UTC, datetime
from typing import Any
from models.execution import ExecutionStep, StepStatus
from kernel.logger import get_logger
from kernel.execution.dispatcher import ExecutionDriver, dispatcher

logger = get_logger(__name__)

class RestN8NDriver(ExecutionDriver):
    """
    Concrete implementation of the REST Driver specifically configured for n8n.
    """
    
    def __init__(self):
        self.base_url = os.getenv("N8N_BASE_URL", "https://n8n.vmserver.app.br")
        self.auth_token = os.getenv("N8N_AUTH_TOKEN", "")
        self.test_mode = os.getenv("N8N_TEST_MODE", "true").lower() == "true" # Default true during development

    async def execute(self, node: ExecutionStep, workspace_id: str) -> None:
        logger.info("driver.rest_n8n.executing", capability=node.capability, node_id=str(node.id))
        
        # In a real implementation, the endpoint and method would come from the capability definition.
        # Since we haven't wired the Capability Model fully into the Node yet, we'll extract it from the payload
        # or assume the capability name matches the endpoint structure.
        
        # Capability naming convention: n8n.communication.send_message
        endpoint_path = node.capability.replace("n8n.", "").replace(".", "/")
        
        webhook_base = "webhook-test" if self.test_mode else "webhook"
        url = f"{self.base_url.rstrip('/')}/{webhook_base}/khonshu/{endpoint_path}"
        
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
            
        payload = node.payload
        payload["workspace_id"] = workspace_id
        
        try:
            async with httpx.AsyncClient() as client:
                logger.debug("driver.rest_n8n.request", url=url, payload=payload)
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()

                if not response.text:
                    raise Exception("A execução no n8n não retornou resposta (body vazio). O workflow pode ter falhado ou o nó 'Respond to Webhook' está faltando.")

                result_data = response.json()
                if isinstance(result_data, dict) and result_data.get("success") is False:
                    error_msg = result_data.get("error", "Erro desconhecido retornado pelo n8n.")
                    raise Exception(f"Erro no n8n: {error_msg}")

                node.output = result_data if isinstance(result_data, dict) else {"result": result_data}
                node.status = StepStatus.COMPLETED.value
                node.finished_at = datetime.now(UTC)
                logger.info("driver.rest_n8n.success", node_id=str(node.id))

        except Exception as e:
            logger.error("driver.rest_n8n.failed", error=str(e), node_id=str(node.id))
            node.output = {"error": str(e)}
            node.status = StepStatus.FAILED.value
            node.finished_at = datetime.now(UTC)

# Register the driver in the global dispatcher
dispatcher.register_driver("rest_n8n", RestN8NDriver())
