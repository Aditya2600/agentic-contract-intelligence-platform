from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from doctask.graph.nodes import NodeDependencies, make_nodes
from doctask.graph.state import GraphState


def build_graph(dependencies: NodeDependencies, checkpointer: Any | None = None):
    builder = StateGraph(GraphState)
    for name, node in make_nodes(dependencies).items():
        builder.add_node(name, node)

    # Nodes return Command(goto=...) for dynamic routing. Do not add static edges
    # to those nodes, otherwise both paths can run.
    builder.add_edge(START, "pin_ruleset")
    builder.add_edge("snapshot_diff_report", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())
