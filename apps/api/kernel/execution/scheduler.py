from datetime import UTC, datetime
from models.execution import ExecutionStep, StepStatus
from kernel.logger import get_logger
from kernel.execution.dispatcher import dispatcher

logger = get_logger(__name__)

class Scheduler:
    """
    Manages timing, retries, and asynchronous transitions.
    Sits between the Runtime (which computes state) and Dispatcher (which executes).
    """
    
    async def schedule(self, node: ExecutionStep, workspace_id: str) -> None:
        """
        Determines when and how to dispatch a node.
        If a node is WAITING, it might put it in PAUSED state.
        If a node is READY, it moves it to RUNNING and dispatches.
        """
        current_status = node.status if node.status is not None else StepStatus.PENDING.value
        logger.info("scheduler.processing", node_id=node.id, status=current_status)
        
        if current_status == StepStatus.READY or current_status == StepStatus.PENDING.value:
            node.status = StepStatus.RUNNING.value
            node.started_at = datetime.now(UTC)
            # Transition to RUNNING and hand off to dispatcher immediately
            await dispatcher.dispatch(node, workspace_id)

        elif current_status == StepStatus.WAITING:
            # E.g. waiting for a webhook or approval
            node.status = StepStatus.PAUSED.value
            logger.info("scheduler.paused", node_id=node.id, reason="waiting for event")

        elif current_status == StepStatus.RETRYING:
            # E.g. apply exponential backoff before dispatching again
            logger.info("scheduler.backoff", node_id=node.id, attempt=node.retry_count)
            # await asyncio.sleep(backoff_time)
            node.status = StepStatus.RUNNING.value
            if node.started_at is None:
                node.started_at = datetime.now(UTC)
            await dispatcher.dispatch(node, workspace_id)
            
        else:
            logger.warning("scheduler.ignored", node_id=node.id, status=current_status)

scheduler = Scheduler()
