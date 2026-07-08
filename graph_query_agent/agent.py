"""Graph Query Agent.

root_agent (orchestrator) delegates to graph_agent, which runs Spanner Graph
GQL for structural code analysis. The graph nodes also carry the generated
summaries, so no separate documentation-search agent is needed.
"""

import os

from google.adk.agents import Agent
from google.adk.tools import ToolContext
from google.cloud import spanner

MODEL = "gemini-3.5-flash"

# Spanner Graph config
SPANNER_INSTANCE = os.environ.get("SPANNER_INSTANCE", "codedoc-instance")
SPANNER_DATABASE = os.environ.get("SPANNER_DATABASE", "codedoc-db")
GRAPH_NAME = os.environ.get("GRAPH_NAME", "code_graph_a")
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "development-459201")

_db = None


def _get_database():
    global _db
    if _db is None:
        client = spanner.Client(project=PROJECT_ID, disable_builtin_metrics=True)
        instance = client.instance(SPANNER_INSTANCE)
        _db = instance.database(SPANNER_DATABASE)
    return _db


def run_gql_query(gql_query: str, tool_context: ToolContext) -> dict:
    """Execute a GQL query against the Spanner code knowledge graph.

    Use this for structural code analysis:
    - File dependencies, class inheritance, method calls
    - Impact analysis, cycle detection, unused code
    - Listing classes, methods, modules, directories

    Args:
        gql_query: A valid GQL query starting with GRAPH code_graph_a.

    Returns:
        dict: Query results or error.
    """
    database = _get_database()
    try:
        results = []
        with database.snapshot() as snapshot:
            result_set = snapshot.execute_sql(gql_query)
            fields = []
            try:
                if hasattr(result_set, 'fields') and result_set.fields:
                    fields = [f.name for f in result_set.fields]
            except Exception:
                pass
            for row in result_set:
                if not fields:
                    try:
                        fields = [f.name for f in result_set.fields]
                    except Exception:
                        fields = [f"col_{i}" for i in range(len(row))]
                row_dict = {}
                for i, val in enumerate(row):
                    key = fields[i] if i < len(fields) else f"col_{i}"
                    row_dict[key] = str(val)[:500] if val is not None else None
                results.append(row_dict)
        return {"status": "success", "query": gql_query, "row_count": len(results), "results": results[:50]}
    except Exception as e:
        return {"status": "error", "query": gql_query, "error_message": str(e)[:500]}


# ===================================================================
# Agent 1: Graph Agent (Spanner Graph GQL)
# ===================================================================

graph_agent = Agent(
    name="graph_agent",
    model=MODEL,
    description=(
        "コードの構造をSpanner Graphで分析するエージェント。"
        "クラス一覧、メソッド一覧、継承関係、依存関係、影響範囲分析、循環依存検出など構造的な質問に使う。"
    ),
    instruction=f"""あなたはSpanner Graphを使ってコードの構造を分析するエージェントです。
ユーザーの質問に対して、GQLクエリを実行して構造的な分析を行ってください。

GQLクエリは必ず `GRAPH {GRAPH_NAME}` で始めてください。

## ノードラベル
- Files (file_id, file_name, extension, directory, summary)
- Classes (class_id, name, file_id, kind, modifiers, summary)
- Methods (method_id, name, class_id, file_id, signature, modifiers, return_type)
- Modules (module_id, name, summary)
- Directories (dir_id, name, summary)

## エッジラベル
- FileDependsOn: (source_file) → (target_file)
- ClassInherits: (child_class) → (parent_class)
- MethodCalls: (caller_method) → (callee_method)
- FileDefinesClass: (file_id) → (class_id)
- ClassDefinesMethod: (class_id) → (method_id)
- FileBelongsToModule: (file_id) → (module_id)
- DirContainsFile: (dir_id) → (file_id)

## クエリ例

### 依存ファイル:
GRAPH {GRAPH_NAME}
MATCH (dep:Files)-[e:FileDependsOn]->(f:Files)
WHERE f.file_name = 'UsersController.php'
RETURN dep.file_name

### クラス継承 (直接+間接):
GRAPH {GRAPH_NAME}
MATCH (child:Classes)-[i:ClassInherits]->{{1,5}}(ancestor:Classes)
WHERE ancestor.name = 'AppController'
RETURN child.name

### メソッド一覧:
GRAPH {GRAPH_NAME}
MATCH (c:Classes)-[d:ClassDefinesMethod]->(m:Methods)
WHERE c.name = 'UsersController'
RETURN m.name, m.signature

### 変更影響分析:
GRAPH {GRAPH_NAME}
MATCH (affected:Files)-[e:FileDependsOn]->{{1,3}}(f:Files)
WHERE f.file_name = 'UsersController.php'
RETURN DISTINCT affected.file_name

### 循環依存:
GRAPH {GRAPH_NAME}
MATCH (a:Files)-[e:FileDependsOn]->{{2,10}}(a)
RETURN DISTINCT a.file_name

日本語で回答してください。""",
    tools=[run_gql_query],
)

# ===================================================================
# Root: Orchestrator
# ===================================================================

root_agent = Agent(
    name="graph_query_agent",
    model=MODEL,
    sub_agents=[graph_agent],
    description="コードベースの構造に関する質問に答えるオーケストレーター。graph_agent に委譲する。",
    instruction="""あなたはコードベースに関する質問に答えるオーケストレーターエージェントです。
質問の種類に応じて、適切なサブエージェントに処理を委譲してください。

## サブエージェントの役割

### graph_agent:
- クラスのメソッド一覧、フィールド一覧
- ファイルの依存関係リスト
- 継承階層の構造的な列挙
- 循環依存の検出
- 変更影響範囲分析
- 数値的・構造的なデータが必要な質問
""",
)
