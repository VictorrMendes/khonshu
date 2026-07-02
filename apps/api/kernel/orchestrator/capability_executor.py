"""CapabilityExecutor — centralized execution layer between Decision
and Generate.

Implements ADR-011: Tool-First Architecture.

Principles:
  - The Kernel decides which tools to run (Decision + IntentRouter).
  - The LLM never calls tools directly.
  - Every capability has exactly one executor path here.
  - All results are collected before the LLM is invoked.
  - The LLM only interprets and synthesizes; the Kernel produces the data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from kernel.logger import get_logger

if TYPE_CHECKING:
    from engines.mission import MissionEngine
    from engines.search import SearchEngine
    from kernel.orchestrator.intent_router import IntentFlags
    from kernel.orchestrator.models import Decision
    from kernel.orchestrator.trace import ReasoningTrace

logger = get_logger("khonshu.capability_executor")


@dataclass
class CapabilityResult:
    """All data collected by the executor for a single request."""

    search_summary: str = ""
    search_count: int = 0
    search_query: str = ""
    search_error_type: str = ""
    docker_summary: str = ""
    calendar_summary: str = ""
    weather_summary: str = ""
    email_summary: str = ""
    checkup_summary: str = ""
    generic_summary: str = ""
    missions_created: list[str] = field(default_factory=list)
    capabilities_used: list[str] = field(default_factory=list)
    internet_sources: int = 0

    def kernel_execution_failed(self) -> bool:
        """True when generic_summary reports a real Kernel-side execution
        failure (not an LLM false-denial). Used to bypass the LLM entirely
        for the final response, since a small local model has been observed
        fabricating plausible-looking fake data instead of reliably
        following the 'admit the failure' instruction in the prompt.
        """
        lower_summary = self.generic_summary.lower()
        return "failed" in lower_summary or "erro" in lower_summary

    def has_tool_output(self) -> bool:
        return bool(
            self.search_summary
            or self.search_error_type
            or self.docker_summary
            or self.calendar_summary
            or self.weather_summary
            or self.email_summary
            or self.checkup_summary
            or self.generic_summary
        )

    def to_prompt_sections(self) -> list[str]:
        """Build prompt sections from tool outputs.

        Each section is labeled so the LLM knows it was produced by the
        Kernel right now — not from training data.
        """
        sections: list[str] = []

        if self.search_count > 0 and self.search_summary:
            sections.append(
                f"## Resultados de Pesquisa Web\n"
                f"(Obtidos pelo Kernel agora para: '{self.search_query}')\n\n"
                f"{self.search_summary}\n\n"
                "Use os resultados acima como fonte primária. "
                "Não afirme que não possui acesso à internet — "
                "a pesquisa já foi executada."
            )
        elif self.search_error_type == "no_results":
            sections.append(
                f"## Pesquisa Web\n"
                f"(Busca por '{self.search_query}'"
                " não retornou resultados)\n\n"
                "A busca foi executada mas não encontrou fontes externas "
                "sobre este tópico. Use seu conhecimento de treinamento "
                "e informe que não foram encontrados resultados atualizados."
            )
        elif self.search_error_type in (
            "timeout", "connection_error", "rate_limit", "provider_error"
        ):
            _label = {
                "timeout": "timeout na requisição",
                "connection_error": "erro de conexão",
                "rate_limit": "limite de requisições atingido",
                "provider_error": "erro no provedor de busca",
            }.get(self.search_error_type, "falha técnica")
            sections.append(
                f"## Pesquisa Web\n"
                f"(Busca por '{self.search_query}' falhou: {_label})\n\n"
                "A busca externa não foi concluída. Responda com seu "
                "conhecimento interno e mencione que não foi possível "
                "confirmar com fontes externas atuais."
            )
        elif self.search_summary:
            sections.append(f"## Pesquisa Web\n{self.search_summary}")

        if self.docker_summary:
            sections.append(
                f"## Estado dos Containers Docker\n{self.docker_summary}"
            )

        if self.calendar_summary:
            sections.append(
                f"## Agenda / Calendário\n{self.calendar_summary}"
            )

        if self.weather_summary:
            sections.append(
                f"## Clima Atual\n{self.weather_summary}"
            )

        if self.email_summary:
            sections.append(
                f"## E-mails\n{self.email_summary}"
            )

        if self.checkup_summary:
            sections.append(
                f"## Status do Sistema\n{self.checkup_summary}"
            )

        if self.generic_summary:
            if self.kernel_execution_failed():
                sections.append(
                    f"## [CRITICAL ERROR] Falha na Execução pelo Kernel\n"
                    f"{self.generic_summary}\n\n"
                    "INSTRUÇÃO CRÍTICA: A execução DA FERRAMENTA FALHOU. "
                    "Você DEVE pedir desculpas e informar ao usuário exatamente o motivo do erro acima. "
                    "SOB NENHUMA HIPÓTESE diga que a ação foi concluída com sucesso."
                )
            else:
                sections.append(
                    f"## Resultados obtidos agora pelo Kernel (Execução de Integração)\n"
                    f"{self.generic_summary}\n\n"
                    "Use estes resultados como fonte exclusiva da resposta para esta integração. "
                    "Se o resultado for um erro (ex: success=false ou error), informe exatamente o erro reportado. "
                    "Não afirme limitações de acesso se os resultados estiverem presentes e forem válidos."
                )

        return sections


class CapabilityExecutor:
    """Executes all capabilities determined by Decision + IntentFlags.

    Injected into CognitiveOrchestrator. Replaces scattered _maybe_search /
    _maybe_create_mission methods with a single authoritative execution step.
    """

    def __init__(
        self,
        search_engine: SearchEngine | None = None,
        mission_engine: MissionEngine | None = None,
        integration_manager=None,
    ) -> None:
        self._search = search_engine
        self._mission = mission_engine
        self._integrations = integration_manager

    async def execute(
        self,
        message: str,
        workspace_id: UUID,
        decision: Decision,
        intent: IntentFlags,
        trace: ReasoningTrace,
        exec_ctx: ExecutionContext | None = None,
        requires_approval: bool = False,
        conversation_id: UUID | None = None,
    ) -> CapabilityResult:
        result = CapabilityResult()

        await self._run_search(
            message, workspace_id, decision, intent, trace, result
        )
        await self._run_integrations(
            workspace_id, decision, intent, trace, result, exec_ctx
        )
        await self._run_capability_fabric(
            message, workspace_id, decision, intent, trace, result, exec_ctx, conversation_id
        )
        await self._run_mission(
            message, workspace_id, decision, intent, trace,
            requires_approval, conversation_id, result,
        )
        await self._run_checkup(workspace_id, intent, trace, result)

        return result

    # ------------------------------------------------------------------ #
    # Search                                                               #
    # ------------------------------------------------------------------ #

    async def _run_search(
        self, message, workspace_id, decision, intent, trace, result
    ) -> None:
        need = decision.need_search or intent.need_search
        if not need:
            trace.skip("search", "SearchEngine", "not needed")
            return
        if self._search is None:
            trace.skip("search", "SearchEngine", "engine unavailable")
            result.search_summary = (
                "A pesquisa foi solicitada, mas o SearchEngine "
                "não está disponível no momento."
            )
            return

        node = trace.begin("search", "SearchEngine")
        try:
            data = await self._search.search(message, workspace_id)
            error_type = data.get("error_type") or ""
            provider = data.get("provider", "unknown")
            result.search_query = message

            if error_type:
                result.search_error_type = error_type
                trace.fail(
                    node,
                    f"error_type={error_type} provider={provider}",
                )
                logger.warning(
                    "capability_executor.search_error",
                    error_type=error_type,
                    provider=provider,
                    query=message,
                )
            else:
                result.search_summary = data.get("summary", "")
                result.search_count = data.get("count", 0)
                result.internet_sources = result.search_count
                result.capabilities_used.append("web_search")
                trace.complete(
                    node,
                    f"count={result.search_count} provider={provider}",
                )
        except Exception as exc:
            trace.fail(node, str(exc))
            logger.warning(
                "capability_executor.search_failed", error=str(exc)
            )
            result.search_error_type = "error"
            result.search_summary = (
                f"A pesquisa encontrou um erro técnico: {exc}."
            )

    # ------------------------------------------------------------------ #
    # Integrations (Docker, Calendar, Weather, Email)                     #
    # ------------------------------------------------------------------ #

    async def _run_integrations(
        self, workspace_id, decision, intent, trace, result, exec_ctx=None
    ) -> None:
        if not intent.need_integrations and not decision.target_capability:
            return

        node = trace.begin("integrations", "IntegrationManager")
        try:
            caps: list[str] = []

            # RC-18B: Execução Genérica Dinâmica
            if decision.target_capability and decision.target_provider:
                try:
                    # Robust capability name (e.g. google.calendar.list_events -> calendar.list_events)
                    target_cap = decision.target_capability
                    if decision.target_provider and target_cap.startswith(f"{decision.target_provider}."):
                        target_cap = target_cap[len(decision.target_provider) + 1:]
                    
                    # LLM hallucination fallback
                    if target_cap == "calendar":
                        target_cap = "calendar.list_events"
                        
                    decision.target_capability = target_cap

                    # ── Resolve Temporal Parameters ──────────────────────
                    temporal_param = decision.capability_params.get("temporal")
                    if temporal_param and isinstance(temporal_param, dict):
                        from kernel.resolvers.temporal import TemporalResolver
                        date_range = temporal_param.get("range")
                        if date_range and exec_ctx:
                            resolution = TemporalResolver.resolve(date_range, exec_ctx)
                            if resolution:
                                decision.capability_params["time_min"] = resolution.time_min
                                decision.capability_params["time_max"] = resolution.time_max
                    
                    data = await self._integrations.execute_capability(
                        workspace_id,
                        decision.target_provider,
                        decision.target_capability,
                        decision.capability_params
                    )
                    import json
                    logger.info(f"[RC-18E] CapabilityExecutor | received data from IntegrationManager")
                    
                    # Check for empty payload to enforce explicit failure
                    is_empty = False
                    if not data:
                        is_empty = True
                    elif isinstance(data, dict):
                        if "items" in data and not data["items"]:
                            is_empty = True
                        elif "events" in data and not data["events"]:
                            is_empty = True
                        elif "error" in data:
                            is_empty = True # Let it be treated as error/empty contextually, wait, actually we want to preserve the error text.
                            
                    if is_empty and "error" not in data:
                        result.generic_summary = f"Execução concluída. Capability '{decision.target_capability}'.\n\nsuccess = false\nerror = 'Provider returned no events or payload was empty.'"
                        logger.info(f"[RC-18E] CapabilityExecutor | injected explicit empty failure")
                    else:
                        result.generic_summary = f"Execução concluída. Capability '{decision.target_capability}'. Retorno:\n```json\n{json.dumps(data, indent=2, ensure_ascii=False)}\n```"
                        
                    caps.append(decision.target_capability)
                except Exception as exc:
                    result.generic_summary = f"Erro ao executar capability '{decision.target_capability}': {exc}"
                    logger.warning(
                        "capability_executor.generic_execution_failed",
                        capability=decision.target_capability,
                        error=str(exc)
                    )
                    caps.append(decision.target_capability)

            if intent.need_docker and not decision.target_capability:
                try:
                    data = await self._integrations.execute_capability(
                        workspace_id, "docker", "docker.list_containers", {}
                    )
                    containers = data.get("containers", [])
                    if not containers:
                        summary = "Nenhum container rodando no momento."
                    else:
                        lines = ["Containers atuais no servidor:"]
                        for c in containers:
                            name = c.get("Names", ["?"])[0].lstrip("/")
                            state = c.get("State", "?")
                            status = c.get("Status", "?")
                            lines.append(f"- **{name}** ({state}): {status}")
                        summary = "\n".join(lines)
                except Exception as exc:
                    summary = f"Erro ao consultar containers Docker: {exc}"
                    
                result.docker_summary = summary
                if summary:
                    caps.append("docker")

            if intent.need_weather and not decision.target_capability:
                summary = await self._query_integration(
                    workspace_id, "weather", "clima"
                )
                result.weather_summary = summary
                if summary:
                    caps.append("weather")

            if intent.need_calendar and not decision.target_capability:
                summary = await self._query_integration(
                    workspace_id, "calendar", "agenda"
                )
                result.calendar_summary = summary
                if summary:
                    caps.append("calendar")

            if intent.need_email and not decision.target_capability:
                summary = await self._query_integration(
                    workspace_id, "email", "e-mails"
                )
                result.email_summary = summary
                if summary:
                    caps.append("email")

            result.capabilities_used.extend(caps)
            trace.complete(node, ", ".join(caps) if caps else "no data")
        except Exception as exc:
            trace.fail(node, str(exc))
            logger.warning(
                "capability_executor.integrations_failed", error=str(exc)
            )

    async def _query_integration(
        self,
        workspace_id: UUID,
        slug_prefix: str,
        label: str,
    ) -> str:
        """Check if an integration is configured and return its status."""
        if self._integrations is None:
            return f"IntegrationManager não disponível para consultar {label}."

        try:
            from engines.integration.base import IntegrationRegistry
            available = IntegrationRegistry.list_all()
            matched_slugs = set()
            for p in available:
                if (slug_prefix in p["slug"].lower()
                    or slug_prefix in p["name"].lower()
                    or any(slug_prefix in cap for cap in p["capabilities"])):
                    matched_slugs.add(p["slug"])

            integrations = await self._integrations.list_integrations(
                workspace_id
            )
            matches = [
                i for i in integrations
                if i.slug in matched_slugs
            ]
        except Exception as exc:
            logger.warning(
                "capability_executor.integration_query_failed",
                slug=slug_prefix,
                error=str(exc),
            )
            return ""

        if not matches:
            return (
                f"Integração '{label}' não está configurada neste workspace. "
                "Acesse Configurações → Integrações para conectar."
            )

        active = [i for i in matches if i.status.value == "active"]
        if not active:
            statuses = ", ".join(
                f"{i.name} ({i.status.value})" for i in matches
            )
            return (
                f"Integração '{label}' está configurada mas inativa: "
                f"{statuses}."
            )

        names = ", ".join(i.name for i in active)
        health = ", ".join(i.health.value for i in active)
        return (
            f"Integração '{label}' ativa: {names}. "
            f"Health: {health}. "
            "Os dados mais recentes estão disponíveis na base de conhecimento."
        )

    # ------------------------------------------------------------------ #
    # Capability Fabric (Phase 11 v3)                                    #
    # ------------------------------------------------------------------ #

    async def _run_capability_fabric(
        self, message, workspace_id, decision, intent, trace, result, exec_ctx, conversation_id
    ) -> None:
        if not decision.need_planner:
            return
            
        node = trace.begin("capability_fabric", "ExecutionPlanner")
        try:
            from kernel.orchestrator.execution_planner import execution_planner
            from kernel.capabilities.discovery import capability_discovery
            from kernel.security.approval_engine import approval_engine
            from kernel.providers.rest_provider import rest_provider

            # Ensure capabilities are loaded (should normally happen on boot)
            if not capability_discovery.definitions:
                capability_discovery.sync()

            plan = await execution_planner.generate_plan(message, {"exec_ctx": exec_ctx})
            
            summary_lines = ["**Execution Fabric Plan Executed:**\n"]
            
            context_permissions = ["auto_write", "auto_execute"] # Mocked permissions for now
            
            for step in plan.plan:
                definition = capability_discovery.get(step.capability)
                if not definition:
                    summary_lines.append(f"- ❌ Step {step.id}: Capability '{step.capability}' not found in registry.")
                    continue
                    
                is_approved = approval_engine.evaluate(definition, context_permissions)
                if not is_approved:
                    summary_lines.append(f"- ⚠️ Step {step.id}: Capability '{step.capability}' blocked by Approval Engine (requires {definition.approval.value}).")
                    continue
                
                # Execute using the appropriate provider
                # For now we route to REST provider hardcoded as requested in architecture
                if definition.provider == "rest_execution_provider":
                    ctx = {"execution_id": str(conversation_id) if conversation_id else "unknown"}
                    exec_result = await rest_provider.execute(definition, step.payload, ctx)
                    summary_lines.append(f"- ✓ Step {step.id} ({step.capability}): {exec_result.get('status')} (ID: {exec_result.get('provider_execution_id')})")
                else:
                    summary_lines.append(f"- ⚠️ Step {step.id}: Unknown provider '{definition.provider}'")

            result.generic_summary += "\n\n" + "\n".join(summary_lines)
            result.capabilities_used.append("capability_fabric")
            trace.complete(node, f"executed {len(plan.plan)} steps in DAG")
            
        except Exception as exc:
            trace.fail(node, str(exc))
            logger.warning("capability_executor.fabric_failed", error=str(exc))

    # ------------------------------------------------------------------ #
    # Mission                                                              #
    # ------------------------------------------------------------------ #

    async def _run_mission(
        self, message, workspace_id, decision, intent, trace,
        requires_approval, conversation_id, result,
    ) -> None:
        need = decision.need_mission or intent.need_mission
        if not need or self._mission is None:
            reason = (
                "not needed" if not need else "engine unavailable"
            )
            trace.skip("create_mission", "MissionEngine", reason)
            return

        node = trace.begin("create_mission", "MissionEngine")
        try:
            from models.mission import MissionTrigger
            mission = await self._mission.create(
                workspace_id=workspace_id,
                intent=message,
                trigger=MissionTrigger.MANUAL,
                requires_approval=requires_approval,
                conversation_id=conversation_id,
            )
            result.missions_created.append(str(mission.id))
            result.capabilities_used.append("mission")
            trace.complete(node, f"mission {str(mission.id)[:8]}…")
        except Exception as exc:
            trace.fail(node, str(exc))
            logger.warning(
                "capability_executor.mission_failed", error=str(exc)
            )

    # ------------------------------------------------------------------ #
    # System check-up                                                      #
    # ------------------------------------------------------------------ #

    async def _run_checkup(
        self, workspace_id, intent, trace, result
    ) -> None:
        if not intent.need_checkup:
            return

        node = trace.begin("checkup", "RuntimeCheckup")
        try:
            lines: list[str] = ["**Status do Sistema Khonshu**\n"]

            lines.append("### Engines")
            lines.append(
                f"- SearchEngine: "
                f"{'✓ disponível' if self._search else '✗ não inicializado'}"
            )
            lines.append(
                f"- MissionEngine: "
                f"{'✓ disponível' if self._mission else '✗ não inicializado'}"
            )
            im_ok = (
                "✓ disponível" if self._integrations
                else "✗ não inicializado"
            )
            lines.append(f"- IntegrationManager: {im_ok}")

            if self._integrations:
                try:
                    integrations = (
                        await self._integrations.list_integrations(
                            workspace_id
                        )
                    )
                    lines.append("\n### Integrações")
                    if integrations:
                        for integ in integrations:
                            icon = (
                                "✓" if integ.status.value == "active"
                                else "✗"
                            )
                            health = getattr(integ, "health", None)
                            health_val = (
                                health.value if health else "unknown"
                            )
                            lines.append(
                                f"- {icon} {integ.name}: "
                                f"status={integ.status.value}, "
                                f"health={health_val}"
                            )
                    else:
                        lines.append(
                            "- Nenhuma integração configurada "
                            "neste workspace."
                        )
                except Exception as exc:
                    lines.append(f"- Erro ao consultar integrações: {exc}")

            result.checkup_summary = "\n".join(lines)
            result.capabilities_used.append("checkup")
            trace.complete(node, "system status built")
        except Exception as exc:
            trace.fail(node, str(exc))
            logger.warning(
                "capability_executor.checkup_failed", error=str(exc)
            )
