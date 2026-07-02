import re
from typing import Any, Dict
from kernel.logger import get_logger
from kernel.orchestrator.ir_compiler import ExecutionIR, IRNode
from kernel.plugins.plugin_manager import plugin_manager

logger = get_logger(__name__)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")


def _normalize(name: str) -> str:
    """Strips punctuation/case so near-miss LLM formatting slips still match.

    e.g. the execution_planner LLM occasionally emits 'n8.n.calendar.list_events'
    instead of 'n8n.calendar.list_events' (a stray '.'); both normalize to
    'n8ncalendarlistevents'.
    """
    return _NON_ALNUM_RE.sub("", name.lower())

class CapabilityResolver:
    """
    Resolves abstract Intent into concrete Capabilities.
    e.g. `Task(type="SendMessage")` -> `n8n.communication.send_message`
    """
    
    async def resolve(self, abstract_ir: ExecutionIR, world_state: Dict[str, Any]) -> ExecutionIR:
        """
        Takes the Optimized Abstract IR and resolves every abstract action
        into a concrete Provider capability based on the active Registry and WorldState.
        """
        logger.info("capability_resolver.resolving")
        
        # Ensure plugins are loaded
        if not plugin_manager.plugins:
            plugin_manager.load_all()
            
        await self._resolve_node(abstract_ir.root, world_state)
        
        return abstract_ir
        
    async def _resolve_node(self, node: IRNode, world_state: Dict[str, Any]) -> None:
        """
        Recursively traverses the IR tree and resolves TASK nodes.
        """
        if node.type == "TASK":
            abstract_intent = getattr(node, "capability", "")
            abstract_lower = abstract_intent.lower()
            abstract_normalized = _normalize(abstract_intent)
            found = False
            normalized_match: str | None = None

            # Search across all loaded plugins
            for plugin_name, manifest in plugin_manager.plugins.items():
                caps = manifest.get("loaded_capabilities", {})
                for cap_key, cap_data in caps.items():
                    cap_name = cap_data.get("name", "")
                    if (cap_data.get("abstract_intent", "").lower() == abstract_lower or
                        cap_name.lower() == abstract_lower or
                        cap_name.replace("n8n.", "").lower() == abstract_lower):
                        node.capability = cap_name
                        found = True
                        logger.debug("capability_resolver.match_found", abstract=abstract_intent, concrete=node.capability)
                        break
                    # Fallback tier: tolerate punctuation/formatting slips from the LLM
                    # (e.g. 'n8.n.calendar.list_events' vs 'n8n.calendar.list_events').
                    if normalized_match is None and _normalize(cap_name) == abstract_normalized:
                        normalized_match = cap_name
                if found:
                    break

            if not found and normalized_match:
                node.capability = normalized_match
                found = True
                logger.warning(
                    "capability_resolver.normalized_match",
                    abstract=abstract_intent,
                    concrete=normalized_match,
                )

            if not found:
                logger.warning("capability_resolver.no_match", abstract=abstract_intent)
                
        elif node.type == "SEQUENCE":
            for child in getattr(node, "nodes", []):
                await self._resolve_node(child, world_state)
                
        elif node.type == "PARALLEL":
            for branch in getattr(node, "branches", []):
                for child in branch:
                    await self._resolve_node(child, world_state)
                    
        elif node.type == "CONDITIONAL":
            for child in getattr(node, "true_branch", []):
                await self._resolve_node(child, world_state)
            for child in getattr(node, "false_branch", []):
                await self._resolve_node(child, world_state)

capability_resolver = CapabilityResolver()
