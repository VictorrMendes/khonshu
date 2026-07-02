from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Dict
from models.execution import ExecutionStep, StepStatus
from kernel.logger import get_logger

logger = get_logger(__name__)

class ExecutionDriver(ABC):
    """
    Base class for protocol-specific drivers (REST, SSH, MCP, etc).
    """
    @abstractmethod
    async def execute(self, node: ExecutionStep, workspace_id: str) -> None:
        pass


class RestDriver(ExecutionDriver):
    """
    Fallback driver for generic (non-n8n) REST capabilities.

    No capability in the current registry uses this path — every capability
    discovered under plugins/*/capabilities today is n8n-prefixed and routed
    to RestN8NDriver instead (see Dispatcher.dispatch). Until a real generic
    REST target exists, this driver fails the node explicitly rather than
    leaving it silently stuck in RUNNING with no output.
    """
    async def execute(self, node: ExecutionStep, workspace_id: str) -> None:
        logger.warning("driver.rest.not_implemented", capability=node.capability, node_id=str(node.id))
        node.output = {"error": f"No generic REST driver implemented for capability '{node.capability}'."}
        node.status = StepStatus.FAILED.value
        node.finished_at = datetime.now(UTC)


class Dispatcher:
    """
    Routes an ExecutionNode to the correct protocol Driver based on the Capability Definition.
    """
    
    def __init__(self):
        self.drivers: Dict[str, ExecutionDriver] = {}

    def register_driver(self, name: str, driver: ExecutionDriver):
        """Dynamically registers a new protocol driver."""
        self.drivers[name] = driver
        logger.info("dispatcher.driver_registered", name=name)

    async def dispatch(self, node: ExecutionStep, workspace_id: str) -> None:
        # We need to determine the protocol.
        # For this test, we assume if the capability starts with 'n8n.', it uses 'rest_n8n'.
        
        protocol = "rest_n8n" if node.capability.startswith("n8n.") else "rest"
        
        driver = self.drivers.get(protocol)
        if not driver:
            logger.error("dispatcher.missing_driver", protocol=protocol)
            # Fail the node if driver is missing
            node.output = {"error": f"Missing driver: {protocol}"}
            node.status = StepStatus.FAILED.value
            node.finished_at = datetime.now(UTC)
            return
            
        await driver.execute(node, workspace_id)

dispatcher = Dispatcher()
