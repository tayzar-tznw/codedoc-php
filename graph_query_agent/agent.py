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

## 重要: リポジトリと厳密な同一性
全ノードは `repo` プロパティ(所属リポジトリ名)を持ちます。複数リポジトリが
同一グラフに同居し、**同名メソッド・同名クラスも repo ごとに別ノード**です。
特定リポジトリに絞るときは常に `WHERE n.repo = '<repo>'` を付けてください。
メソッドは `fqmn`(例 `App\\Model\\Table\\UsersTable::save`)、クラスは `fqcn` で
一意に識別できます — 単純名(name)は repo・namespace 間で重複し得ます。

## ノードラベル
- Files (file_id, file_name, extension, directory, path, origin, repo, summary)
- Classes (class_id, name, namespace, fqcn, file_id, kind, modifiers, start_line, end_line, origin, repo, summary)
- Methods (method_id, name, class_id, file_id, fqmn, signature, modifiers, return_type, start_line, end_line, origin, repo, summary)
- Modules (module_id, name, repo, summary)
- Directories (dir_id, name, repo, summary)
- DbTables (table_id, name, columns[JSON], indexes[JSON], foreign_keys[JSON], source_file, plugin, repo, summary)

## エッジラベル
- FileImports: (source_file) → (target) — 解決済み use インポート
- FileDependsOn: (source_file) → (target_file)
- ClassInherits: (child_class) → (parent_class)  ※ kind = extends|implements|uses
- MethodCalls: (caller_method) → (callee_method)  ※ resolution = lsp|convention:<rule>(解決済みのみ)
- PossiblyCalls: (caller_method) → (callee_method)  ※ reason = ambiguous|dynamic|name-heuristic(未確定の候補)
- FileDefinesClass: (file_id) → (class_id)
- ClassDefinesMethod: (class_id) → (method_id)
- FileBelongsToModule: (file_id) → (module_id)
- DirContainsFile: (dir_id) → (file_id)
- TableReferences: (source_table) → (target_table)  ※ 外部キー(fk_column, referenced_column)
- ClassMapsToTable: (class_id) → (table_id)  ※ CakePHP Table クラス→DBテーブル(via = settable|convention)

確定した呼び出しは MethodCalls、未確定は PossiblyCalls です。厳密な呼び出し関係が
必要なときは MethodCalls を、可能性を広く見たいときは PossiblyCalls も併用してください。

## クエリ例

### 依存ファイル(特定リポジトリ内):
GRAPH {GRAPH_NAME}
MATCH (dep:Files)-[e:FileDependsOn]->(f:Files)
WHERE f.file_name = 'UsersController.php' AND f.repo = 'web'
RETURN dep.file_name, dep.repo

### メソッドの確定した呼び出し先(FQMNで一意特定):
GRAPH {GRAPH_NAME}
MATCH (caller:Methods)-[e:MethodCalls]->(callee:Methods)
WHERE caller.fqmn = 'App\\Controller\\UsersController::index'
RETURN callee.fqmn, e.resolution

### クラス継承 (直接+間接、リポジトリ内):
GRAPH {GRAPH_NAME}
MATCH (child:Classes)-[i:ClassInherits]->{{1,5}}(ancestor:Classes)
WHERE ancestor.name = 'AppController' AND ancestor.repo = 'web'
RETURN child.fqcn

### Table クラスが対応する DB テーブル:
GRAPH {GRAPH_NAME}
MATCH (c:Classes)-[m:ClassMapsToTable]->(t:DbTables)
WHERE c.name = 'UsersTable' AND c.repo = 'web'
RETURN t.name, m.via

### 同名メソッドがどのリポジトリに存在するか:
GRAPH {GRAPH_NAME}
MATCH (m:Methods)
WHERE m.name = 'save'
RETURN m.repo, m.fqmn

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
