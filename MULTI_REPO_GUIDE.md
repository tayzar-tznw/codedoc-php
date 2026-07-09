# 複数リポジトリ利用ガイド（Multi-Repository Guide）

複数のリポジトリを **1 つの Spanner グラフに同居**させて解析・クエリするための手順と
仕様をまとめたドキュメントです。**リポジトリ間で同名のメソッド・クラスが衝突して
統合されることはありません**（後述のノード同一性を参照）。

- 関連ドキュメント: [README.md](README.md)（全体の使い方） / [GRAPH_SCHEMA_GUIDE.md](GRAPH_SCHEMA_GUIDE.md)（グラフスキーマ詳細）

## 目次
1. [概要とモデル（独立した島）](#1-概要とモデル独立した島)
2. [なぜ必要か — 同名シンボルの衝突問題](#2-なぜ必要か--同名シンボルの衝突問題)
3. [ノード同一性（衝突しない仕組み）](#3-ノード同一性衝突しない仕組み)
4. [使い方](#4-使い方)
5. [マニフェスト形式（`--repos`）](#5-マニフェスト形式---repos)
6. [出力・チェックポイントの分離](#6-出力チェックポイントの分離)
7. [クエリ（GQL）](#7-クエリgql)
8. [整合性の確認（`validate`）](#8-整合性の確認validate)
9. [既存グラフからの移行と注意点](#9-既存グラフからの移行と注意点)
10. [エンドツーエンドの例](#10-エンドツーエンドの例)
11. [FAQ・制約](#11-faq制約)

---

## 1. 概要とモデル（独立した島）

複数リポジトリは、1 つの Spanner データベース・1 つのプロパティグラフの中に **repo ごとの
部分グラフ（独立した島 / isolated islands）**として格納されます。

- 各ノードは所属リポジトリを表す **`repo` プロパティ**を持ちます。
- リポジトリ **A から B のシンボルへの参照**は、vendor と同じく **`external`（確定済み・
  グラフ外、エッジなし・集計のみ）**として扱われます。リポジトリをまたぐエッジは
  **構造的に発生しません**（誤接続ゼロ）。
- 投入は **リポジトリ単位**で行い、同じグラフに追記していきます。後からリポジトリを追加・
  再投入しても、他リポジトリのノードには影響しません。

```
                Spanner DB / 1 プロパティグラフ (code_graph_a)
   ┌─────────────────────────┐   ┌─────────────────────────┐
   │  repo = "web"           │   │  repo = "api"           │
   │  Files/Classes/Methods… │   │  Files/Classes/Methods… │
   │  （web 内で解決したエッジ）│   │  （api 内で解決したエッジ）│
   └─────────────────────────┘   └─────────────────────────┘
        A→B の参照は external（エッジは張られない）
```

## 2. なぜ必要か — 同名シンボルの衝突問題

CakePHP のような規約ベースのフレームワークでは、リポジトリが違っても
`App\Model\Table\UsersTable::save` や `src/Controller/UsersController.php` のように
**相対パス・FQCN・メソッド名が完全に一致**することが普通に起こります。

repo の区別が無いと、これらは同じノード ID にハッシュされて **1 ノードに統合**され、
「repo web の save」と「repo api の save」が混ざってしまいます。呼び出し解決も
別リポジトリの同名シンボルに誤って結び付く恐れがあります。本機能はこれを防ぎます。

## 3. ノード同一性（衝突しない仕組み）

ノード ID は次の要素からハッシュされます（`graph_generator/pipeline.py` の `_make_id`）:

```
ID = ID_PREFIX + sha256( repo | 種別 | ターゲット相対パス | FQCN | メンバー名 )[:16]
```

- **`repo`** が先頭に入るため、相対パス・FQCN・メンバー名がリポジトリ間で一致しても
  **必ず別ノード**になります。
- `ID_PREFIX` は従来どおり「同一 DB 内で並存する別グラフ」を分ける名前空間です。`repo` は
  「1 グラフ内でリポジトリを分ける」次元で、両者は併用されます。
- 全ノードテーブル（Files / Classes / Methods / Modules / Directories / DbTables）に
  `repo` カラムがあります。
- メソッドは `fqmn`（例 `App\Controller\UsersController::index`）、クラスは `fqcn` で
  一意に識別できます。単純名（`name`）は repo・名前空間をまたいで重複し得ます。

> スキーマ上のカラム定義は [GRAPH_SCHEMA_GUIDE.md](GRAPH_SCHEMA_GUIDE.md) を参照してください。

## 4. 使い方

`--repo-name` と `--repos` は `analyze` / `generate wiki` / `generate graph` で使えます。

### 4-1. 逐次投入（インクリメンタル）

リポジトリを 1 つずつ、同じグラフに追記します。後からの追加・再投入も可能です。

```bash
python -m graph_generator analyze ./web --repo-name web
python -m graph_generator analyze ./api --repo-name api
python -m graph_generator analyze ./shared-lib --repo-name shared-lib
```

- `--repo-name` を省略すると、**対象ディレクトリのベース名**がリポジトリ名になります
  （例: `./web` → `web`）。
- ドキュメントのみ / グラフのみも同様に repo を指定できます:
  ```bash
  python -m graph_generator generate wiki  ./web --repo-name web
  python -m graph_generator generate graph ./web --repo-name web
  ```

### 4-2. 一括投入（マニフェスト）

複数リポジトリをまとめて投入します。内部では 1 リポジトリずつ順番に実行されます。

```bash
python -m graph_generator analyze --repos repos.json
```

## 5. マニフェスト形式（`--repos`）

JSON 配列で、各要素は `{name?, path, include_vendor?}`。`name` 省略時はディレクトリ名、
`include_vendor` 省略時は通常の判定（CLI フラグ → `.env INCLUDE_VENDOR` → 対話プロンプト）に
従います。文字列（パスのみ）の要素も受け付けます。

```json
[
  {"name": "web", "path": "./web", "include_vendor": false},
  {"name": "api", "path": "./api"},
  {"name": "billing", "path": "/abs/path/to/billing", "include_vendor": true},
  "./tools"
]
```

- 相対パスはコマンド実行時のカレントディレクトリ基準で解決されます。
- `include_vendor` を各リポジトリで個別に指定できます（vendor の扱いは
  [README の該当節](README.md#vendor-ディレクトリの取り扱い)を参照）。

## 6. 出力・チェックポイントの分離

各リポジトリの中間生成物・チェックポイントは **`OUTPUT_DIR/repos/<repo>/`** 配下に
分離されるため、リポジトリ同士が互いを上書きしません。

```
output_docs_pipeline/
└── repos/
    ├── web/
    │   ├── summaries/            # ファイル/ディレクトリ要約
    │   ├── entities.json         # tree-sitter 抽出（Phase 1.5）
    │   ├── resolutions.json      # LSP 解決結果（Phase 1.6, mtime レジューム）
    │   ├── graph_checkpoint.json # Phase 8-10 のレジューム
    │   ├── pipeline_data.pkl     # docs→graph 受け渡し
    │   └── lsp_cache/            # Intelephense のインデックス
    └── api/
        └── …
```

- Intelephense（LSP）は **リポジトリごとに別ワークスペース**でインデックスされます。
  したがって repo A の解決は A のシンボルだけを見て行われ、B のシンボルに誤って
  解決されることはありません（独立した島が構造的に保証される理由）。

## 7. クエリ（GQL）

特定リポジトリに絞るときは、常に `WHERE n.repo = '<repo>'` を付けます。厳密な同一性が
必要なときは `fqmn` / `fqcn` を使ってください（単純名は repo 間で重複し得ます）。

```gql
-- web リポジトリ内の、UsersController.php が依存するファイル
GRAPH code_graph_a
MATCH (dep:Files)-[e:FileDependsOn]->(f:Files)
WHERE f.file_name = 'UsersController.php' AND f.repo = 'web'
RETURN dep.file_name, dep.repo
```

```gql
-- 特定メソッド（FQMN で一意特定）の確定した呼び出し先
GRAPH code_graph_a
MATCH (caller:Methods)-[e:MethodCalls]->(callee:Methods)
WHERE caller.fqmn = 'App\Controller\UsersController::index'
RETURN callee.fqmn, callee.repo, e.resolution
```

```gql
-- 同名メソッド 'save' がどのリポジトリに存在するか（衝突していないことの確認）
GRAPH code_graph_a
MATCH (m:Methods)
WHERE m.name = 'save'
RETURN m.repo, m.fqmn
```

- `MethodCalls` は**確定した呼び出しのみ**（`resolution = lsp | convention:<rule>`）。
  未確定の候補は `PossiblyCalls`（`reason = ambiguous | dynamic | name-heuristic`）に入ります。
- リポジトリをまたぐ呼び出しはエッジになりません（`external` として集計のみ）。GQL で
  リポジトリ横断のエッジが返ることは無い、という前提でクエリを書けます。

## 8. 整合性の確認（`validate`）

```bash
python -m graph_generator validate
```

テーブル別の行数と孤立エッジ検査に加えて、**「Nodes by repo」= リポジトリ別のノード件数**を
表示します。投入漏れ・同名リポジトリの取り違え（意図せぬ統合）がひと目で分かります。

```
  Nodes by repo:
  Files                     web=812, api=430
  Classes                   web=640, api=355
  Methods                   web=5,102, api=2,880
  …
```

## 9. 既存グラフからの移行と注意点

`repo` 次元の導入により **ノード ID の体系が変わりました**（`ID_SCHEME` を更新）。既存の
（repo 導入前の）グラフを使っている場合は、次のいずれかで作り直してください。

- **推奨**: 新しい `GRAPH_NAME` / `ID_PREFIX`（`.env`）で作り直し、各リポジトリを投入し直す。
- もしくは `python -m graph_generator.setup_spanner_graph --destroy` の後に
  `setup spanner` で作り直す。

既存 DB に対しては次で `repo` カラム追加を**冪等に**行えます（`INFORMATION_SCHEMA` 差分から
`ALTER TABLE ... ADD COLUMN` のみ発行）。ただし `--migrate` はスキーマだけを更新するため、
**旧 ID 体系で書かれた既存の行はそのまま残ります**（insert_or_update のため二重化し得る）。
クリーンな状態にするには上記の作り直しを推奨します。

```bash
python -m graph_generator.setup_spanner_graph --migrate
```

## 10. エンドツーエンドの例

```bash
# 0) 設定（.env）— 全リポジトリで同じ Spanner インスタンス/DB/GRAPH_NAME を共有
python -m graph_generator init

# 1) Spanner リソース作成（既存なら不足分だけ冪等に追加）
python -m graph_generator setup spanner

# 2) 複数リポジトリを 1 グラフへ投入（逐次 or 一括のどちらでも）
python -m graph_generator analyze ./web --repo-name web
python -m graph_generator analyze ./api --repo-name api
#   または: python -m graph_generator analyze --repos repos.json

# 3) 整合性チェック（repo 別件数・孤立エッジ 0 を確認）
python -m graph_generator validate

# 4) クエリ（ADK REPL / MCP / Web UI いずれか）
adk run graph_query_agent
```

グラフを Spanner に書き込む前に、GCP 不要でローカルにグラフ内容を検証したい場合は
`python -m graph_generator evaluate`（フィクスチャに対する精度評価）を利用できます。

## 11. FAQ・制約

- **Q. リポジトリ間で本当に同名クラス/メソッドが混ざらない?**
  A. 混ざりません。ノード ID が `repo` を含むため別ノードになります。これは
  `tests/test_multi_repo.py` で「同一ソースを 2 つの repo として投入してもノード ID 集合が
  完全に素（disjoint）」「どのエッジも repo 境界を越えない」ことを検証済みです。

- **Q. 別リポジトリの共有ライブラリを呼んでいる。エッジは張られる?**
  A. 現状は「独立した島」モデルのため張られません（`external` として集計のみ）。
  リポジトリをまたぐ依存を 1 本のエッジで見たい場合は将来的な拡張になります。

- **同名ディレクトリのリポジトリ**: `--repo-name` を省略するとベース名が repo 名になるため、
  異なるパスでもディレクトリ名が同じだと同一 repo とみなされ統合されます。**必ず
  `--repo-name` を明示**してください。`validate` の repo 別件数で取り違えに気付けます。

- **vendor**: vendor はリポジトリごとに既定で除外されます。取り込みは各リポジトリ単位で
  フラグ / `.env` / マニフェストの `include_vendor` で制御します。

- **ドキュメントビューア（webapp）**: 現状は単一 `OUTPUT_DIR` を配信する作りのため、複数
  リポジトリのブラウズ UI は本ガイドの対象外です（グラフ + クエリ層が対象）。
