import asyncio
import os
import sys

# Add apps/api to path
sys.path.append(os.path.abspath("apps/api"))

from kernel.orchestrator.execution_planner import execution_planner
from kernel.providers.ollama import OllamaProvider
from kernel.plugins.plugin_manager import plugin_manager
from pydantic import BaseModel

class DummyTask(BaseModel):
    id: str
    description: str

async def test():
    # Load plugins
    plugin_manager.load_all()
    
    # Init LLM
    llm = OllamaProvider(model="qwen2.5:3b")
    
    # Create a task
    tasks = [DummyTask(id="task_1", description="Adicione a tarefa 'Comprar mantimentos para a semana' no todoist com a descrição 'Incluir frutas, legumes e proteínas'.")]
    
    # Run
    ir = await execution_planner.generate_plan(tasks, {"llm": llm})
    
    print("Generated IR:")
    for node in ir.root.nodes:
        print(f"Capability: {node.capability}")
        print(f"Payload: {node.payload}")

if __name__ == "__main__":
    asyncio.run(test())
