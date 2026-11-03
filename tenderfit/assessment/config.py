from dataclasses import dataclass


@dataclass(frozen=True)
class PipelineConfig:
    name: str
    rules: bool = True       # deterministic conditions and decomposition
    guard: bool = True       # the model's verdict is checked against facts and citations
    hybrid: bool = True      # lexical + vector + rerank; otherwise vector only
    coverage: bool = True
    agent: bool = False
    targeted: bool = True
    graph: bool = True       # follow references and definitions the requirement names
    corrective: bool = True  # search again for unresolved references, and abstain when evidence stays insufficient


# Experiment arms. Each is reachable from a setting; none has been shown to beat another until an experiment says so.
BASELINES = {
    "B0": PipelineConfig("B0", rules=False, guard=False, graph=False, corrective=False),
    "B1": PipelineConfig("B1", graph=False, corrective=False),
    "B2": PipelineConfig("B2", graph=False),
    "B3": PipelineConfig("B3", corrective=False),
    "B4": PipelineConfig("B4", agent=True, graph=False, corrective=False),
    "B5": PipelineConfig("B5", agent=True),
    "B5-full": PipelineConfig("B5-full", agent=True, targeted=False),
}
