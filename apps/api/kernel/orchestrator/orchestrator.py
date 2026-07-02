"""CognitiveOrchestrator — the executive brain of Khonshu.

ADR-011: Tool-First Architecture.

Pipeline:
  1. Build base context  (ContextBuilder — memory, knowledge, RAG)
  2. Route intent        (IntentRouter — deterministic keyword analysis)
  3. Decide routing      (DecisionEngine LLM — merges IntentRouter flags)
  4. Execute caps        (CapabilityExecutor — search, integrations, missions)
  5. Generate response   (LLMProvider — only interprets Kernel outputs)
  6. Validate response   (CapabilityValidator — reject hallucinated limits)
  7. Learn               (LearningEngine)

The LLM never calls tools directly. It receives data produced by the Kernel
and synthesizes a response. The Kernel is the source of truth.
"""
from __future__ import annotations

import contextlib
import re
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from uuid import UUID

from kernel.logger import get_logger
from kernel.providers.base import ChatMessage, LLMProvider

from .capability_executor import CapabilityExecutor, CapabilityResult
from .decision import DecisionEngine
from .intent_router import IntentFlags, IntentRouter
from .learning import LearningEngine
from .models import (
    ConversationResult,
    ExecutionContext,
    LearningAction,
    LearningActionType,
    LearningDecision,
    OrchestratorRequest,
    OrchestratorResponse,
    RiskLevel,
    TraceNode,
)
from .trace import ReasoningTrace

TokenCallback = Callable[[str], Awaitable[None]]

logger = get_logger("khonshu.orchestrator")

# Phrases the LLM must never emit when the capability is active
_INVALID_SEARCH_PHRASES = [
    r"não tenho acesso à internet",
    r"nao tenho acesso a internet",
    r"não possuo acesso à internet",
    r"sem acesso à internet",
    r"não consigo acessar a internet",
    r"não tenho capacidade de buscar",
    r"não posso pesquisar",
]
_INVALID_DOCKER_PHRASES = [
    r"não consigo acessar containers",
    r"não tenho acesso ao docker",
    r"não posso monitorar containers",
]
_INVALID_CALENDAR_PHRASES = [
    r"não tenho (?:acesso (?:à|a)|integração de) (?:minha )?agenda",
    r"não consigo acessar (?:o )?calendário",
    r"não possuo calendário",
    r"sem acesso (?:ao )?calendário",
    r"não tenho integração de calendário",
    # Second-person framing ("você não possui...") and the English
    # "Calendar" wording (e.g. "Google Calendar") slip past the
    # first-person-only patterns above.
    r"(?:não|nao)\s+(?:possui|possuo|tenho|tem)\s+(?:uma\s+)?integra[çc][ãa]o.{0,80}?(?:calend[aá]rio|calendar)",
    r"integra[çc][ãa]o.{0,80}?(?:calend[aá]rio|calendar).{0,40}?(?:não|nao)\s+(?:est[áa]|foi)\s+configurad",
    r"precis(?:a|o)\s+(?:que\s+)?(?:você\s+)?configur\w*.{0,60}?(?:calend[aá]rio|calendar)",
]
_INVALID_WEATHER_PHRASES = [
    r"não consigo verificar (?:o )?clima",
    r"não tenho (?:acesso|dados) (?:a(?:o)?|sobre) (?:o )?clima",
    r"não possuo dados (?:de|do|sobre) clima",
    r"sem (?:acesso a(?:o)?|dados de) (?:o )?clima",
]
# Always invalid — regardless of which tool ran
_INVALID_GENERAL_PHRASES = [
    r"sou apenas (?:um )?(?:modelo|assistente|chatbot|ia) de linguagem",
    r"sou apenas um sistema local",
    r"não possuo (?:essa |esta )?capacidade",
    r"sou incapaz de",
    r"não tenho (?:essa |esta )?capacidade",
]


class CognitiveOrchestrator:
    """Coordinates all cognitive engines for a single user request."""

    def __init__(
        self,
        context_builder,
        decision_engine: DecisionEngine,
        learning_engine: LearningEngine,
        llm_provider: LLMProvider,
        capability_executor: CapabilityExecutor | None = None,
        memory_engine=None,
        knowledge_engine=None,
        metrics=None,
        session_factory=None,
        trace_broadcaster=None,
    ) -> None:
        self._context = context_builder
        self._decision = decision_engine
        self._learning = learning_engine
        self._llm = llm_provider
        self._executor = capability_executor or CapabilityExecutor()
        self._memory = memory_engine
        self._knowledge = knowledge_engine
        self._metrics = metrics
        self._sessions = session_factory
        self._trace_broadcaster = trace_broadcaster
        self._intent_router = IntentRouter()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    async def execute(
        self,
        request: OrchestratorRequest,
        on_step: Callable[[TraceNode], Awaitable[None]] | None = None,
        on_token: TokenCallback | None = None,
    ) -> OrchestratorResponse:
        if not request.workspace_id:
            raise ValueError("workspace_id is required")

        workspace_id = UUID(request.workspace_id)
        trace_id = request.conversation_id
        trace = ReasoningTrace(
            workspace_id=request.workspace_id,
            trace_id=trace_id,
            broadcaster=self._trace_broadcaster
        )
        
        try:

            # ── 0. Create Execution Context ──────────────────────────────────
            from kernel.state.world_state import world_state_store
            current_world_state = await world_state_store.get_state(workspace_id)
            
            exec_ctx = ExecutionContext(
                now=datetime.now(timezone.utc),
                timezone="America/Sao_Paulo", # Defaulting for now, could be dynamic per workspace
                locale="pt_BR",
                workspace_id=str(workspace_id),
                world_state=current_world_state
            )
            
            conv_id = (
                UUID(request.conversation_id)
                if request.conversation_id else None
            )

            # ── 1. Check for Suspended Execution (ResumeEngine) ──────────────
            from kernel.orchestrator.resume_engine import ResumeEngine
            resume_engine = ResumeEngine(self._sessions)
            interaction_token = request.metadata.get("interaction_token") if hasattr(request, "metadata") and request.metadata else None
            
            resumed, resume_response = await resume_engine.try_resume(
                message=request.message,
                workspace_id=str(workspace_id),
                conversation_id=str(conv_id) if conv_id else None,
                interaction_token=interaction_token
            )
            
            if resumed:
                trace.finish(success=True)
                
                # Persist the user message and the system response
                await self._persist_conversation(request, resume_response)
                
                # Fast-path return
                return OrchestratorResponse(
                    response=resume_response,
                    decision=None, 
                    trace=trace.nodes,
                    confidence=1.0,
                    risk=RiskLevel.low,
                    sources=[],
                    capabilities_used=[],
                    missions_created=[],
                    learning_actions=[],
                    thinking_steps=trace.thinking_steps(),
                    memory_used=0,
                    knowledge_used=0,
                    internet_sources=[],
                    integrations_used=[],
                    planner_used=False,
                    mission_created=False,
                    estimated_cost=0.0,
                    estimated_time=0.0,
                    approval_required=False,
                )

            # ── 2. Build base context ────────────────────────────────────────
            ctx = await self._build_context(request, workspace_id, trace)
            if on_step:
                await on_step(trace.nodes[-1])

            # ── 2. Route intent (deterministic) ─────────────────────────────
            intent = self._route_intent(request.message, trace)
            if on_step:
                await on_step(trace.nodes[-1])

            # ── 3. Decide routing (LLM, merged with intent flags) ────────────
            decision = await self._decide(request, intent, exec_ctx, trace)
            if on_step:
                await on_step(trace.nodes[-1])

            # ── 5. Execute capabilities (Cognitive Kernel) ───────────────────────
            requires_approval = (
                decision.need_confirmation
                or decision.risk_level != RiskLevel.low
            )
            
            # Legacy Execution has been completely REMOVED (Phase 14).
            # The LLM no longer selects concrete capabilities.
            
            # New Definitive Cognitive Kernel Path
            requires_execution = (
                decision.need_planner or 
                decision.need_integrations or 
                decision.need_search or 
                decision.need_execution
            )
            
            if requires_execution:
                goal = decision.goal or request.message
                node = trace.begin("cognitive_kernel", "ExecutionRuntime")
                try:
                    from kernel.orchestrator.strategy_planner import strategy_planner
                    from kernel.orchestrator.execution_planner import execution_planner
                    from kernel.orchestrator.execution_optimizer import execution_optimizer
                    from kernel.orchestrator.capability_resolver import capability_resolver
                    from kernel.orchestrator.ir_compiler import ir_compiler
                    from kernel.execution.execution_runtime import execution_runtime
                    
                    # 1. Strategy Planner (Goal -> Abstract Tasks)
                    strategy = await strategy_planner.generate_strategy(goal, {"exec_ctx": exec_ctx, "llm": self._llm})
                    
                    # 2. Execution Planner (Abstract Tasks -> Abstract Execution IR)
                    abstract_ir = await execution_planner.generate_plan(strategy.tasks, {"exec_ctx": exec_ctx, "llm": self._llm})
                    
                    # 3. Execution Optimizer (Abstract IR -> Optimized Abstract IR)
                    optimized_ir = execution_optimizer.optimize(abstract_ir)
                    
                    # 4. Capability Resolver (Optimized Abstract IR -> Concrete IR)
                    concrete_ir = await capability_resolver.resolve(optimized_ir, exec_ctx.world_state)
                    
                    # 5. Compile IR -> ExecutionGraph (Nodes)
                    from models.execution import Execution, StepStatus, Interaction, InteractionType
                    import uuid
                    execution_id = uuid.uuid4()
                    
                    execution = Execution(
                        id=execution_id,
                        workspace_id=workspace_id,
                        goal=goal,
                        status="RUNNING"
                    )
                    
                    steps = ir_compiler.compile(concrete_ir, str(execution_id))
                    
                    if self._sessions:
                        async with self._sessions() as session:
                            session.add(execution)
                            for step in steps:
                                session.add(step)
                            await session.commit()
                    
                    # 6. State Runtime Execution
                    suspended_question = None
                    results = []
                    for step in steps:
                        await execution_runtime.execute_node(step, str(workspace_id))
                        
                        if step.status == StepStatus.WAITING_INPUT.value:
                            from kernel.orchestrator.clarification_engine import ClarificationEngine
                            
                            clarification_engine = ClarificationEngine(self._llm)
                            missing_params = getattr(step, '_missing_parameters', [])
                            
                            question = await clarification_engine.generate_question(
                                capability_name=step.capability,
                                capability_description=step.capability, 
                                missing_parameters=missing_params
                            )
                            
                            # Create and Persist Interaction
                            interaction = Interaction(
                                interaction_type=InteractionType.CLARIFICATION.value,
                                execution_id=step.execution_id,
                                step_id=step.id,
                                missing_parameters=[p.to_dict() for p in missing_params],
                                question=question,
                                conversation_id=conv_id,
                                workspace_id=workspace_id
                            )
                            
                            if self._sessions:
                                async with self._sessions() as session:
                                    # Use merge because step is from another session or detached
                                    session.add(interaction)
                                    execution.status = "WAITING_INPUT"
                                    await session.merge(execution)
                                    await session.merge(step)
                                    await session.commit()
                                
                            results.append(question)
                            suspended_question = question
                            break # Halt execution of further steps
                            
                        elif step.status == StepStatus.FAILED.value:
                            error = (step.output or {}).get("error", "unknown error")
                            results.append(f"Action '{step.capability}' failed: {error}")
                        else:
                            results.append(f"Action '{step.capability}' result: {step.output}")
                            
                    cap_result = CapabilityResult(
                        generic_summary="\\n".join(results) if results else "Kernel execution completed with no output.",
                        calendar_summary="\\n".join(results) if any("calendar" in r.lower() for r in results) else ""
                    )
                        
                    trace.complete(node, f"compiled {len(steps)} nodes via Graph IR")
                except Exception as exc:
                    trace.fail(node, str(exc))
                    logger.warning("orchestrator.cognitive_kernel_failed", error=str(exc))
                    cap_result = CapabilityResult(generic_summary=f"Kernel execution failed: {exc}")
            else:
                cap_result = CapabilityResult()
                    
            if on_step:
                await on_step(trace.nodes[-1])

            # ── 6. Generate response ─────────────────────────────────────────
            # A real Kernel-side execution failure bypasses the LLM entirely:
            # a small local model has been observed ignoring the explicit
            # "admit the failure, never fabricate success" instruction and
            # inventing plausible-looking fake data instead. A deterministic
            # message guarantees honesty for this case at the cost of a less
            # natural response.
            kernel_failed = cap_result.kernel_execution_failed()
            if suspended_question:
                response_text = suspended_question
            elif kernel_failed:
                response_text = (
                    "Não consegui concluir essa ação — houve uma falha na "
                    f"execução: {cap_result.generic_summary}"
                )
                gen_node = trace.begin("generate", "DeterministicFailureResponse")
                trace.complete(gen_node, "bypassed LLM: kernel execution failed")
                if on_token:
                    await on_token(response_text)
            else:
                response_text = await self._generate(
                    request, ctx, cap_result, trace, on_token
                )
                if on_step:
                    await on_step(trace.nodes[-1])

            # ── 7. Validate response ─────────────────────────────────────────
            if not suspended_question and not kernel_failed:
                response_text = await self._validate_response(
                    response_text, cap_result, intent, trace
                )

            # ── 8. Learn ─────────────────────────────────────────────────────
            learning_actions = await self._maybe_learn(
                request, decision, response_text, ctx,
                cap_result.missions_created, workspace_id, trace,
            )
            if on_step:
                await on_step(trace.nodes[-1])

            orch_response = OrchestratorResponse(
                response=response_text,
                decision=decision,
                trace=trace.nodes,
                confidence=decision.confidence,
                risk=decision.risk_level,
                sources=[],
                capabilities_used=cap_result.capabilities_used,
                missions_created=cap_result.missions_created,
                learning_actions=learning_actions,
                thinking_steps=trace.thinking_steps(),
                memory_used=ctx.memory_count,
                knowledge_used=ctx.knowledge_count,
                internet_sources=cap_result.internet_sources,
                integrations_used=[],
                planner_used=decision.need_planner,
                mission_created=bool(cap_result.missions_created),
                estimated_cost=decision.estimated_cost,
                estimated_time=decision.estimated_latency,
                approval_required=decision.need_confirmation,
            )

            await self._persist_conversation(request, response_text)
            trace.finish(success=True)
            return orch_response
            
        except Exception as exc:
            trace.finish(success=False, error=str(exc))
            raise

    # ------------------------------------------------------------------ #
    # Pipeline steps                                                       #
    # ------------------------------------------------------------------ #

    async def _build_context(self, request, workspace_id, trace):
        node = trace.begin("build_context", "ContextBuilder")
        _metrics_ctx = (
            self._metrics.time_context_build()
            if self._metrics else contextlib.nullcontext()
        )
        with _metrics_ctx:
            ctx = await self._context.build(
                workspace_id=workspace_id,
                user_message=request.message,
            )
        trace.complete(
            node,
            f"{ctx.memory_count} mem, "
            f"{ctx.knowledge_count} facts, "
            f"{ctx.chunk_count} chunks",
        )
        if self._metrics:
            self._metrics.record_memory_hit(ctx.memory_count)
            self._metrics.record_knowledge_hit(ctx.knowledge_count)
        return ctx

    def _route_intent(
        self, message: str, trace: ReasoningTrace
    ) -> IntentFlags:
        node = trace.begin("intent_route", "IntentRouter")
        intent = self._intent_router.analyze(message)
        trace.complete(node, intent.summary() or "none")
        return intent

    async def _decide(self, request, intent: IntentFlags, exec_ctx: ExecutionContext, trace):
        node = trace.begin("decide", "DecisionEngine")
        _metrics_ctx = (
            self._metrics.time_planning()
            if self._metrics else contextlib.nullcontext()
        )
        with _metrics_ctx:
            decision = await self._decision.decide(request.message, exec_ctx)

        # Merge: IntentRouter flags take priority (OR semantics)
        if intent.need_search:
            decision.need_search = True
        if intent.need_integrations:
            decision.need_integrations = True
        if intent.need_mission:
            decision.need_mission = True
            decision.need_planner = True
        if intent.need_memory:
            decision.need_memory = True

        label = decision.reason or f"risk={decision.risk_level.value}"
        if intent.detected:
            label = f"intent=[{intent.summary()}] {label}"
        trace.complete(node, label)
        return decision

    async def _generate(
        self,
        request,
        ctx,
        cap_result: CapabilityResult,
        trace,
        on_token: TokenCallback | None = None,
    ):
        node = trace.begin("generate", "LLMProvider")

        prompt_parts = [ctx.system_prompt]
        prompt_parts.extend(cap_result.to_prompt_sections())
        enriched_prompt = "\n\n".join(prompt_parts)

        messages = [
            ChatMessage(role="system", content=enriched_prompt),
            ChatMessage(role="user", content=request.message),
        ]
        try:
            if on_token is not None:
                chunks: list[str] = []
                async for chunk in self._llm.chat_stream(messages):
                    chunks.append(chunk)
                    await on_token(chunk)
                response_text = "".join(chunks)
            else:
                result = await self._llm.chat(messages)
                response_text = result.content

            trace.complete(node, f"{len(response_text)} chars")
            return response_text
        except Exception as exc:
            trace.fail(node, str(exc))
            raise

    async def _validate_response(
        self,
        response: str,
        cap_result: CapabilityResult,
        intent: IntentFlags,
        trace: ReasoningTrace,
    ) -> str:
        """Reject responses that falsely claim capability limitations.

        Checks: search denial when search ran, docker denial when docker
        data was provided, calendar/weather denials, and always-invalid
        general defensive phrases when any tool output exists.
        """
        invalid = False
        violated_cap = "unknown"

        # Search: invalid if internet WAS accessible (real results or no_results)
        search_accessible = (
            bool(cap_result.search_summary and cap_result.search_count > 0)
            or cap_result.search_error_type == "no_results"
        )
        if search_accessible:
            for pattern in _INVALID_SEARCH_PHRASES:
                if re.search(pattern, response, re.IGNORECASE):
                    invalid = True
                    violated_cap = "search"
                    break

        if (cap_result.docker_summary or (cap_result.generic_summary and "docker" in cap_result.generic_summary.lower())) and not invalid:
            for pattern in _INVALID_DOCKER_PHRASES:
                if re.search(pattern, response, re.IGNORECASE):
                    invalid = True
                    violated_cap = "docker"
                    break

        if (cap_result.calendar_summary or (cap_result.generic_summary and "calendar" in cap_result.generic_summary.lower())) and not invalid:
            for pattern in _INVALID_CALENDAR_PHRASES:
                if re.search(pattern, response, re.IGNORECASE):
                    invalid = True
                    violated_cap = "calendar"
                    break

        # Positive check: if real calendar events exist, the response must
        # reference at least one of them. Catches any denial phrasing at
        # all -- not just the ones enumerated above -- because it checks
        # for the presence of real data instead of the absence of specific
        # words. Three distinct denial phrasings slipped past the phrase
        # blacklist in production before this was added.
        if not invalid:
            event_titles = cap_result.calendar_event_titles()
            if event_titles and not any(
                title.lower() in response.lower() for title in event_titles
            ):
                invalid = True
                violated_cap = "calendar_data_ignored"

        if (cap_result.weather_summary or (cap_result.generic_summary and "weather" in cap_result.generic_summary.lower())) and not invalid:
            for pattern in _INVALID_WEATHER_PHRASES:
                if re.search(pattern, response, re.IGNORECASE):
                    invalid = True
                    violated_cap = "weather"
                    break

        if cap_result.has_tool_output() and not invalid:
            for pattern in _INVALID_GENERAL_PHRASES:
                if re.search(pattern, response, re.IGNORECASE):
                    invalid = True
                    violated_cap = "general"
                    break

        if invalid:
            node = trace.begin("validate", "CapabilityValidator")
            _msg = f"denied {violated_cap} capability — corrected"
            trace.fail(node, _msg)
            logger.warning(
                "orchestrator.invalid_response_corrected",
                violated_cap=violated_cap,
            )
            sections = cap_result.to_prompt_sections()
            if sections:
                return (
                    "**[Resposta corrigida pelo Kernel]**\n\n"
                    + "\n\n".join(sections)
                )

        return response

    async def _persist_conversation(
        self, request: OrchestratorRequest, response_text: str
    ) -> None:
        if self._sessions is None or not request.conversation_id:
            return
        try:
            from models.conversation import (  # noqa: F401
                Conversation,
                Message,
                MessageRole,
            )

            conv_id = UUID(request.conversation_id)
            async with self._sessions() as session:
                conv = await session.get(Conversation, conv_id)
                if conv is None:
                    return
                session.add(Message(
                    conversation_id=conv_id,
                    role=MessageRole.user,
                    content=request.message,
                ))
                session.add(Message(
                    conversation_id=conv_id,
                    role=MessageRole.assistant,
                    content=response_text,
                ))
                await session.commit()
        except Exception as exc:
            logger.warning(
                "orchestrator.persist_failed", error=str(exc)
            )

    async def _maybe_learn(
        self,
        request,
        decision,
        response_text,
        ctx,
        missions_created,
        workspace_id,
        trace,
    ):
        if not decision.need_learning:
            trace.skip("learn", "LearningEngine", "not needed")
            return []

        node = trace.begin("learn", "LearningEngine")
        try:
            conv_result = ConversationResult(
                request=request,
                decision=decision,
                response=response_text,
                memories_retrieved=ctx.memory_count,
                knowledge_count=ctx.knowledge_count,
                missions_created=missions_created,
            )
            ld = await self._learning.decide(conv_result)
            applied: list[LearningAction] = []
            if ld.should_learn and ld.actions:
                applied = await self._apply_learning(ld, workspace_id)
            trace.complete(
                node,
                f"{len(applied)} applied — {ld.reason}",
            )
            return applied
        except Exception as exc:
            trace.fail(node, str(exc))
            logger.warning("orchestrator.learning_failed", error=str(exc))
            return []

    # ------------------------------------------------------------------ #
    # Learning application                                                 #
    # ------------------------------------------------------------------ #

    async def _apply_learning(
        self,
        ld: LearningDecision,
        workspace_id: UUID,
    ) -> list[LearningAction]:
        applied: list[LearningAction] = []
        for action in ld.actions:
            if action.type == LearningActionType.ignore:
                continue
            try:
                await self._persist_action(action, workspace_id)
                applied.append(action)
            except Exception as exc:
                logger.warning(
                    "orchestrator.learning_persist_failed",
                    type=action.type.value,
                    error=str(exc),
                )
        return applied

    async def _persist_action(
        self, action: LearningAction, workspace_id: UUID
    ) -> None:
        from models.memory import MemoryType

        t = action.type

        if t == LearningActionType.update_preference and self._memory:
            await self._memory.remember(
                workspace_id=workspace_id,
                content=action.content,
                type=MemoryType.semantic,
                confidence=action.confidence,
                importance=0.9,
                source="orchestrator",
            )

        elif t == LearningActionType.record_pattern and self._memory:
            await self._memory.remember(
                workspace_id=workspace_id,
                content=action.content,
                type=MemoryType.semantic,
                confidence=action.confidence,
                importance=0.7,
                source="orchestrator",
            )

        elif t == LearningActionType.create_memory and self._memory:
            await self._memory.remember(
                workspace_id=workspace_id,
                content=action.content,
                type=MemoryType.episodic,
                confidence=action.confidence,
                importance=0.6,
                source="orchestrator",
            )

        elif t == LearningActionType.create_fact and self._knowledge:
            await self._knowledge.store_fact(
                workspace_id=workspace_id,
                statement=action.content,
                source_type="orchestrator_learning",
                confidence=action.confidence,
            )

        elif (
            t == LearningActionType.create_observation
            and self._knowledge
        ):
            await self._knowledge.store_observation(
                workspace_id=workspace_id,
                statement=action.content,
                derived_from="orchestrator_learning",
                confidence=action.confidence,
                expires_in_days=7,
            )
