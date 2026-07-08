# CodeDoc — ソースコード自動ドキュメント生成システム

Tree-sitter による AST 解析と Vertex AI（Gemini）を組み合わせたパイプラインで、ソースコードから日本語の Wiki ドキュメントを自動生成するシステムです。さらに、Cloud Spanner 上にコードのナレッジグラフを構築することで、自然言語による質問応答や継承関係・呼出しチェーンといった構造的クエリ（graph-RAG）を可能にします。

中核となる実装は **`graph_generator/`** パッケージで、Google Gen AI SDK（`google-genai`）を直接利用する 10 フェーズパイプラインとして提供されます。解析対象は **PHP / CakePHP プロジェクト**に特化しており、tree-sitter による PHP の構造抽出に加えて、CakePHP のビューテンプレート（`.ctp`）の解析と Phinx マイグレーション（`config/Migrations/`）からの DB スキーマ検出に対応しています。

---

## 目次

- [システム概要](#システム概要)
- [前提条件](#前提条件)
- [セットアップ](#セットアップ)
- [モデルの変更方法](#モデルの変更方法)
- [対応言語と除外ディレクトリ](#対応言語と除外ディレクトリ)
- [実行方法](#実行方法)
- [データベーススキーマ カテゴリ（自動生成）](#データベーススキーマ-カテゴリ自動生成)
- [出力結果の構造](#出力結果の構造)
- [出力例の詳細](#出力例の詳細)
- [webapp をローカルで実行する](#webapp-をローカルで実行する)
- [MCP サーバー](#mcp-サーバー)
- [パイプライン構成](#パイプライン構成)
- [ファイル構成](#ファイル構成)

---

## システム概要

CodeDoc は **10 フェーズ** のパイプラインでソースコードを解析し、以下を自動生成します。

| 生成物 | 内容 |
|-------|------|
| **ファイル要約** | 各ソースファイルの目的・クラス・メソッドの日本語要約 |
| **ディレクトリ要約** | ディレクトリ単位の上位要約（ボトムアップで集約） |
| **モジュール（トピック）ドキュメント** | 機能モジュールごとのドキュメント（Mermaid 図・コンポーネント表付き） |
| **reasoning_index.json** | 要約ツリー＋トピックツリーの構造化データ |
| **index.md** | プロジェクト全体の概要ドキュメント |
| **metadata.json** | 生成メタデータ（タイムスタンプ、モデル、統計） |
| **Spanner ナレッジグラフ** | Files / Classes / Methods / Modules / Directories のノードと 7 種類のエッジ + `text-embedding-005` ベクトル |

```
ソースコード
   ↓ (Tree-sitter で AST 解析 → エンティティ抽出)
   ↓ (Gemini で要約・トピック抽出・モジュールドキュメント生成)
   ↓ (Spanner にグラフ書込み + Vertex AI text-embedding-005 でベクトル化)
Wiki 用ドキュメント + ナレッジグラフ（graph-RAG クエリの基盤）
```

クエリは `graph_query_agent`（Spanner コードグラフに対する GQL 実行エージェント）が担当し、`mcp_server/` および `webapp/` のチャットから利用できます（詳細は後述の各セクションを参照）。

詳細な 10 フェーズの仕様は **[`graph_generator/manual.md`](graph_generator/manual.md)** を参照してください。

---

## 前提条件

| 項目 | 要件 |
|------|------|
| Python | 3.12 以上（リポジトリ自身は `uv` で 3.14 を使用していますが、3.12+ で動作します） |
| Google Cloud | プロジェクトが作成済みであること |
| Vertex AI API | `aiplatform.googleapis.com` を有効化済みであること |
| gcloud CLI | インストール・認証済みであること |
| Cloud Spanner | ナレッジグラフ（graph-RAG クエリ）用に Enterprise 階層のインスタンスが必要 |

### Google Cloud の準備

```bash
# gcloud CLI のインストール（未インストールの場合）
# https://cloud.google.com/sdk/docs/install

# ログイン
gcloud auth login
gcloud auth application-default login

# プロジェクト設定
gcloud config set project YOUR_PROJECT_ID

# Vertex AI API の有効化
gcloud services enable aiplatform.googleapis.com
```

---

## セットアップ

### 1. リポジトリのクローンと依存関係のインストール

```bash
git clone <repository-url>
cd codedoc

# 仮想環境の作成（推奨）
python -m venv .venv
source .venv/bin/activate    # macOS/Linux
# .venv\Scripts\activate     # Windows

# 依存関係のインストール（フルパイプライン用）
pip install -r requirements.txt
```

`requirements.txt` には Google Gen AI SDK（`google-genai`）、Cloud Spanner クライアント、tree-sitter 本体と PHP 文法パッケージ（`tree-sitter-php`）、その他必要なライブラリがピン留めされた状態で含まれています。インストール内容の詳細は同ファイルを参照してください。

### 2. 環境変数の設定

設定はプロジェクトルートの `.env` ファイルから読み込まれます。テンプレートとして `.env.example` が用意されているのでコピーして編集してください。

```bash
cp .env.example .env
```

`.env` の主要なキー（`.env.example` がそのままテンプレート）:

```bash
# --- Required: Google Cloud ---
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_GENAI_USE_VERTEXAI=true

# --- Gemini ---
GEMINI_MODEL=gemini-3.5-flash
GEMINI_CONCURRENCY=100

# --- Spanner Graph ---
SPANNER_INSTANCE=codedoc-instance
SPANNER_DATABASE=codedoc-db
# GRAPH_NAME=code_graph_a

# --- Output ---
OUTPUT_DIR=output_docs_pipeline
EMBED_CONCURRENCY=20

# GCE 上の SSL mTLS 問題を回避する場合のみ有効化
GCE_METADATA_MTLS_MODE=none
```

対話的に `.env` を生成したい場合は以下のコマンドが利用できます。

```bash
python -m graph_generator init
```

> **注意**: `GOOGLE_CLOUD_PROJECT` は実際の Google Cloud プロジェクト ID に置き換えてください。Vertex AI を経由するため `GOOGLE_GENAI_USE_VERTEXAI=true` は必須です。

---

## モデルの変更方法

使用する Gemini モデルは環境変数で制御します。

### ドキュメント生成パイプライン

`graph_generator/config.py` は `GEMINI_MODEL` 環境変数を読み込みます（デフォルト: `gemini-3.5-flash`）。

```bash
# .env で変更
GEMINI_MODEL=gemini-3.5-flash
```

```python
# graph_generator/config.py（参考）
MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash")
```

### クエリエージェント

`graph_query_agent/agent.py` のモジュール定数で設定されます。

```python
MODEL = "gemini-3.5-flash"  # ← ここを変更
```

### 利用可能なモデル

| モデル | 特徴 | 推奨用途 |
|--------|------|----------|
| `gemini-3-pro-preview` | 高品質な推論・生成 | 重要プロジェクトのドキュメント生成 |
| `gemini-3.5-flash` | 高速・低コスト・バランス型 | ドキュメント生成（デフォルト）／クエリ応答 |
| `gemini-2.5-pro-preview-06-05` | 安定版 | 品質重視の場合 |
| `gemini-2.5-flash-preview-05-20` | 安定版・高速 | コスト重視の場合 |

> **ヒント**: 並行数（`GEMINI_CONCURRENCY`、デフォルト 100）が大きいほどスループットは上がりますが、Vertex AI のクォータに注意してください。レート制限が頻発する場合は `10`〜`30` 程度まで下げます。

---

## 対応言語と除外ディレクトリ

### 対応するソースファイル拡張子（2 種類）

以下の拡張子を持つファイルが自動的にスキャン・解析されます。

| カテゴリ | 拡張子 |
|---------|--------|
| **PHP** | `.php` |
| **CakePHP ビューテンプレート** | `.ctp`（CakePHP 3 以前のテンプレート） |

スキャンされたファイルはすべて Phase 1.5 で tree-sitter（`tree-sitter-php`）による構造抽出の対象になります。抽出されるのは、クラス／インターフェイス／トレイト／enum（ケース含む）、メソッド（可視性・`static`・`abstract`・`final` の修飾子、パラメータ、戻り値型付き）、クラスプロパティ、継承関係（`extends`／`implements`／トレイト `use`）、`use` インポートと `require`/`include`、名前空間です。トップレベル関数は `(global)` 擬似クラスに集約され、メソッド呼び出しエッジは `foo()`・`$obj->foo()`・`$obj?->foo()`・`Foo::bar()` の各形式から収集されます。HTML が混在する PHP も扱えるフル文法（`language_php()`）を使用しているため、CakePHP テンプレートもそのまま解析できます。

### 自動除外されるディレクトリ

以下のディレクトリはスキャン対象から自動的に除外されます（先頭が `.` で始まるディレクトリも自動的にスキップされます）。

```
.git, .svn, .hg, node_modules, __pycache__,
.idea, .vscode, build, dist, bin,
venv, .venv, vendor, tmp, logs, webroot
```

> **CakePHP プロジェクトの場合**: Composer の `vendor`、CakePHP の `tmp`・`logs`・`webroot`・`bin` が自動除外されるため、依存パッケージ・一時ファイル・静的アセット・`cake` コンソールはスキャンされません。一方 `config/Migrations` は除外されずスキャン対象となり、後述の [データベーススキーマ カテゴリ](#データベーススキーマ-カテゴリ自動生成) の検出に利用されます。

### カスタマイズ

対応拡張子や除外ディレクトリを変更したい場合は、**`graph_generator/config.py`** の `SOURCE_EXTENSIONS` および `SKIP_DIRS` を編集してください。

```python
# graph_generator/config.py

# PHP only. `.ctp` covers legacy CakePHP (≤3) view templates — plain PHP syntax.
SOURCE_EXTENSIONS = {
    ".php", ".ctp",
}

# `vendor` (Composer), `tmp`/`logs`, and `webroot` (assets + front controller)
# are CakePHP noise; `bin` holds only the `cake` console bootstrap.
# NOTE: `config/Migrations` must stay scannable — DB_SCHEMA_DETECTORS below
# relies on it.
SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__",
    ".idea", ".vscode", "build", "dist",
    "bin", "venv", ".venv",
    "vendor", "tmp", "logs", "webroot",
}
```

---

## 実行方法

CLI は `python -m graph_generator <command>` の形式で起動します。代表的なコマンドは以下のとおりです。

| コマンド | 説明 | 実行フェーズ |
|---------|------|-------------|
| `init` | 対話的に `.env` を生成 | -- |
| `setup spanner` | Spanner インスタンス + データベース + テーブル + プロパティグラフを作成 | -- |
| `generate wiki <target_dir>` | **ドキュメントのみ**生成（→ `output_docs_pipeline/`） | Phase 1〜6 |
| `generate graph <target_dir>` | **Spanner グラフのみ**生成（pickle がなければ Phase 1 + 1.5 を自動実行） | Phase 1, 1.5, 8〜10 |
| `analyze <target_dir>` | **フルパイプライン**（ドキュメント + グラフ + エンベディング） | Phase 1〜10 |
| `upload graph <target_dir>` | グラフを Spanner にアップロード（`generate graph` のエイリアス） | Phase 8〜10 |
| `validate` | Spanner グラフの行数カウントと孤立エッジ検出を行い整合性を検証 | -- |

### 推奨ワークフロー

```bash
# 1. 設定生成（対話的）
python -m graph_generator init

# 2. Spanner リソースを作成
python -m graph_generator setup spanner

# 3. ドキュメント＋グラフを一気に生成
python -m graph_generator analyze /path/to/your/source

# 4. Spanner グラフの整合性を検証
python -m graph_generator validate
```

### 単独実行のパターン

```bash
# ドキュメントだけ欲しい（グラフ機能は不要）
python -m graph_generator generate wiki /path/to/source

# 既にドキュメントは生成済みで、グラフだけ追加したい
python -m graph_generator generate graph /path/to/source
```

`generate wiki` 完了時にパイプライン状態が `output_docs_pipeline/pipeline_data.pkl` に保存されるため、`generate graph` はそれを読み込んでグラフ生成だけを実行できます。pickle が無い場合は Phase 1（スキャン）と Phase 1.5（tree-sitter）を自動で再実行し、ディスク上に既に存在する要約をロードしてからグラフ生成に進みます。

### パイプラインの 10 フェーズ（概要）

| Phase | 名称 | 概要 |
|-------|------|------|
| 1 | File Scanner | 対象ディレクトリを再帰走査しソースファイルを収集 |
| 1.5 | Tree-sitter Entities | AST 解析でクラス／メソッド／インポート等をローカル抽出（API 呼出しゼロ） |
| 2 | File Summaries | Gemini で各ファイルの要約を並行生成 |
| 3 | Dir Summaries | ボトムアップでディレクトリ要約を生成（深さレベル別並行） |
| 4 | Topic Extraction | ファイル要約から機能モジュール（トピック）を識別 |
| 5 | Topic Summaries | 各トピックの包括ドキュメント（Mermaid 図付き）を生成 |
| 6 | Index Assembly | `reasoning_index.json` / `index.md` / `metadata.json` を出力 |
| 7 | （欠番） | 旧 LLM ベースのエンティティ抽出は Phase 1.5 の tree-sitter に置換済み |
| 8 | Write Graph Nodes | Files / Classes / Methods / Modules / Directories を Spanner に書込み |
| 9 | Write Graph Edges | 7 種類のエッジ（FileDependsOn 等）を 7 並行で書込み |
| 10 | Generate Embeddings | `text-embedding-005` で Files / Classes / Modules のベクトルを生成 |

レジューム（途中中断後の再実行）、リトライ機構、エラーログの仕様などの詳細は **[`graph_generator/manual.md`](graph_generator/manual.md)** を参照してください。

> **処理時間の目安**: 30 ファイル程度のプロジェクトで数分、数千ファイルのリポジトリは並行数とモデル選択次第で十数分〜1 時間程度になります。Phase 1.5（tree-sitter）は API を一切使用しないため、37,000 ファイル規模でも約 40 秒で完了します。

> **テストフィクスチャ**: 動作確認用のサンプル（CakePHP アプリなど）は `test_codes/` 配下に置いて使います（`test_codes/` は `.gitignore` により Git 管理外、各環境でローカルに用意します）。本番では自分のプロジェクトディレクトリのパスを `<target_dir>` に指定してください。

---

## データベーススキーマ カテゴリ（自動生成）

解析対象のコードベースに DB スキーマ／マイグレーションファイルが含まれている場合、通常のトピック（LLM が推論する機能モジュール）に加えて、**`データベーススキーマ`** カテゴリが決定論的に自動追加され、**専用の `schema/` ディレクトリ**に展開されます。

- **`output_docs_pipeline/schema/index.md`** — スキーマ全体の概要、Mermaid `erDiagram` による ER 図、各テーブルへのリンク付きの **テーブル一覧表**。
- **`output_docs_pipeline/schema/<テーブル名>.md`** — テーブルごとに 1 ページ。**カラム**（名前／型／制約／説明の表）、**インデックス**、**外部キー**、**リレーション**を記載。
- **`output_docs_pipeline/topics/データベーススキーマ.md`** — `index.md` のモジュール一覧から辿れる**ポインターページ**。`schema/index.md` と各テーブルページへのリンク集として機能します（旧バージョンの単一 ER ページの代わり）。Spanner グラフ側でも他のトピックと同様に `Modules` ノードとして取り込まれます。

生成は **1 回の構造化 LLM 抽出 → 純粋な Python による決定論的レンダリング** で完結します（Phase 4 と Phase 5 の間で走る `phase_schema_docs` が担当）。Mermaid 構文やテーブル列は LLM 出力ではなく抽出済みデータから組み立てるため、出力が安定し、テーブル数が増えても LLM コストはほぼ一定です。抽出が失敗した場合は Phase 5 の `DB_SCHEMA_PROMPT` 分岐で単一ページのドキュメントを生成するフォールバックがあり、`schema/index.md` が既に存在する場合は再生成をスキップしてレジューム可能です。

現在は **CakePHP (Phinx Migrations)** に対応しています（`config/Migrations/` 配下の `.php` マイグレーション — `Migrations\AbstractMigration` を継承するクラス — を検出します）。他のフレームワーク（Rails、Prisma、Django、Raw SQL DDL など）への対応は `graph_generator/config.py` の `DB_SCHEMA_DETECTORS` に検出器辞書を 1 つ追加するだけで拡張でき、`pipeline.py` の変更は不要です。詳細は **[`graph_generator/manual.md`](graph_generator/manual.md#データベーススキーマ-カテゴリ)** を参照してください。

> **注意**: 検出は Phase 1 でスキャンされたファイルのみが対象です。`.sql` などデフォルトでスキャン対象外の拡張子を使うフレームワークを追加する場合は、同じ `config.py` の `SOURCE_EXTENSIONS` への追加が前提になります。また、このカテゴリと `schema/` ディレクトリは docs パイプライン（Phase 4〜6 + `phase_schema_docs`）で生成されるため、ドキュメント未生成のリポジトリに `generate graph` を直接実行した場合はグラフに現れません。

---

## 出力結果の構造

`generate wiki` または `analyze` を実行すると、`$OUTPUT_DIR`（既定 `output_docs_pipeline/`）以下に次の構造でドキュメントが生成されます。

```
output_docs_pipeline/
├── index.md                              # プロジェクト全体の概要
├── reasoning_index.json                  # 構造化データ（機械可読）
├── metadata.json                         # 生成メタデータ
├── entities.json                         # Phase 1.5 で抽出した tree-sitter エンティティ
├── pipeline_data.pkl                     # フェーズ間でのデータ受け渡し（generate graph がこれを再利用）
├── timing_report.json                    # 各フェーズの実行時間レポート
├── summaries/
│   ├── files/
│   │   ├── src_Controller_UsersController.php.md  # 各ソースファイルの要約
│   │   └── ...
│   └── dirs/
│       ├── src.md                        # 各ディレクトリの要約
│       └── ...
├── topics/
│   ├── topic_tree.json                   # トピック構造定義
│   ├── ユーザー管理機能.md                # モジュール（トピック）ドキュメント
│   ├── アプリケーション基盤.md            # モジュール（トピック）ドキュメント
│   ├── データベーススキーマ.md            # （DB スキーマ検出時）schema/ へのポインターページ
│   └── ...
└── schema/                               # phase_schema_docs で生成（DB スキーマ検出時のみ）
    ├── index.md                          # 概要 + Mermaid erDiagram + テーブル一覧
    ├── users.md                          # テーブルごとに 1 ページ
    └── ...                               #   （カラム / インデックス / 外部キー / リレーション）
```

ファイル名はパス区切りをアンダースコアに変換した形（例: `src/Controller/UsersController.php` → `src_Controller_UsersController.php.md`）になります。

---

## 出力例の詳細

### ファイル要約の例

`output_docs_pipeline/summaries/files/<ファイル名>.md` には、Gemini が生成した数段落の日本語要約が記録されます。

> このファイル `UsersController.php` は、CakePHP アプリケーションのユーザー管理機能を担うコントローラークラス `UsersController` を定義しています。`App\Controller` 名前空間に属し、共通基底クラスである `AppController` を継承して、`index` と `view` の 2 つのアクションを公開しています。
>
> `index` アクションは `UsersTable` の `find('active')` で有効なユーザーの一覧を取得し、`paginate` によるページネーションを適用してビューへ渡します。`view` アクションは URL パラメータ `$id` を受け取り、`$this->Users->get($id)` で単一の `User` エンティティを取得して詳細画面を表示します。

### モジュールドキュメントの例

`output_docs_pipeline/topics/<トピック名>.md` には次のような情報が自動生成されます。

**1. クラス関係図（Mermaid）**

```mermaid
classDiagram
    class UsersController {
        +index()
        +view($id = null) ?Response
    }
    class UsersTable {
        +initialize()
        +findActive()
    }
    class User
    AppController <|-- UsersController
    Table <|-- UsersTable
    Entity <|-- User
    UsersController ..> UsersTable : find / get
    UsersTable ..> User : Creates
```

**2. コンポーネント表**

| コンポーネント | 責務 |
|-----------|----------------|
| **UsersController** | `index`（一覧）と `view`（詳細）のアクションを提供する HTTP リクエストの受け口。 |
| **UsersTable** | `users` テーブルへのアクセスと `findActive` カスタムファインダーを提供するテーブルクラス。 |

**3. データフロー図（Mermaid）**

```mermaid
sequenceDiagram
    participant Browser as ブラウザ
    participant UsersController
    participant UsersTable
    participant View as templates/Users/index.php

    Browser->>UsersController: GET /users
    UsersController->>UsersTable: find('active')
    UsersTable-->>UsersController: SelectQuery
    UsersController->>UsersController: paginate(query)
    UsersController->>View: set(compact('users'))
    View-->>Browser: ユーザー一覧の HTML を描画
```

**4. 依存関係（他モジュールへのリンク）**

- **[アプリケーション基盤](アプリケーション基盤.md)**: `UsersController` は共通基底クラス `AppController` を継承しています。
- **[データベーススキーマ](データベーススキーマ.md)**: `users` テーブルの定義は `config/Migrations/` の Phinx マイグレーション（`CreateUsers`）に由来します。

### index.md の例

プロジェクト全体の概要ドキュメントで、高レベルのアーキテクチャ図とモジュールリンクが含まれます。

```mermaid
graph TD
    M0["アプリケーション基盤"]
    M1["ユーザー管理機能"]
    M2["データベーススキーマ"]
    M1 --> M2
```

### metadata.json の例

```json
{
  "generation_info": {
    "timestamp": "2026-04-10T12:00:00.000000",
    "model": "gemini-3.5-flash",
    "generator": "pipeline_v2 (google-genai)",
    "repo_path": "/path/to/source"
  },
  "statistics": {
    "total_files": 200,
    "total_directories": 30,
    "total_topics": 8
  }
}
```

---

## webapp をローカルで実行する

`webapp/` は **FastAPI（バックエンド）+ Vite/React 19（フロントエンド）** で構築されたドキュメントビューア兼チャット UI です。生成済みのドキュメントをブラウザで閲覧でき、`graph_query_agent`（Spanner コードグラフに GQL を実行する構造解析エージェント）を使ったチャット応答機能も組み込まれています。

### ドキュメントの読み込み元

`webapp/main.py` の `DOCS_DIR` は、パイプライン本体（`graph_generator/config.py`）と同じ `OUTPUT_DIR` 環境変数を参照します（既定値も同じく `output_docs_pipeline/`）。webapp は起動時にリポジトリルートの `.env` を読み込むため、`generate wiki` や `analyze` で生成したドキュメントは追加設定なしでそのまま表示されます。

出力先を変更している場合は、パイプライン実行時と webapp 起動時で同じ `OUTPUT_DIR` をリポジトリルートの `.env` に設定し、両者の値を揃えてください。

> **注意**: チャットタブは `graph_query_agent` を直接呼び出すため、Spanner コードグラフ（`codedoc-instance` / `codedoc-db` / `code_graph_a`）が構築済み（`setup spanner` → `analyze` → `upload graph`）である必要があります。

### バックエンド（FastAPI）

```bash
# リポジトリルートから
pip install -r webapp/requirements.txt

# webapp はリポジトリルートの .env に続けて graph_query_agent/.env を自動で
# 読み込みます（dotenv は既存の値を上書きしません）。そのため graph_query_agent/.env に
# Vertex AI 設定が入っていれば、ルート .env が無くても API キー不要（Vertex AI / ADC）で
# 動作します。ルート .env を置いた場合はそちらが優先されます。
#
# 最低限の設定（ルート .env もしくは graph_query_agent/.env のいずれか）:
# GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
# GOOGLE_GENAI_USE_VERTEXAI=true   # ← これが無いと Gemini Developer API 扱いになり API キーを要求される
#
# チャット機能（graph_query_agent）を使うには、Spanner コードグラフが
# 構築済みである必要があります。グラフの接続先は環境変数で指定します
# （既定値は実インスタンスと一致するため通常は設定不要）:
#   - SPANNER_INSTANCE=codedoc-instance
#   - SPANNER_DATABASE=codedoc-db
#   - GRAPH_NAME=code_graph_a

# 開発サーバーを起動
uvicorn webapp.main:app --reload --port 8000
```

これで `http://localhost:8000` で FastAPI サーバーが起動します。

### フロントエンド（Vite 開発サーバー）

開発時はホットリロードが効く Vite 開発サーバーを使うのが便利です。

```bash
cd webapp/frontend
npm install
npm run dev
```

`http://localhost:5173` をブラウザで開きます。Vite の `vite.config.js` で `/api` リクエストは `http://localhost:8000` にプロキシされる設定になっているため、バックエンドを `port 8000` で並行起動しておけばそのまま動作します。

### フロントエンド（本番ビルド・シングルサーバー構成）

本番運用や、フロントエンドとバックエンドを 1 つのポートで配信したい場合はビルド成果物を FastAPI から配信できます。

```bash
cd webapp/frontend
npm run build      # → webapp/frontend/dist/ に成果物が生成される

# あとはバックエンドだけ起動すれば OK
cd ../..
uvicorn webapp.main:app --port 8000
```

`webapp/main.py` の末尾は `frontend/dist/` が存在する場合に限り `/` 配下に静的ファイルをマウントするようになっているので、`http://localhost:8000` をブラウザで開けば SPA がそのまま表示されます。

### 主な API エンドポイント

| メソッド | パス | 概要 |
|---------|------|------|
| `GET` | `/api/docs/index` | `index.md` を Markdown + HTML レンダリング結果として返す |
| `GET` | `/api/docs/tree` | トピック／ファイル要約／ディレクトリ要約の一覧をツリー構造で返す |
| `GET` | `/api/docs/topics` | `topic_tree.json` の中身を返す |
| `GET` | `/api/docs/topics/{topic_name}` | 指定トピックのドキュメントを返す |
| `GET` | `/api/docs/summaries/{files|dirs}/{name}` | ファイル／ディレクトリ要約を返す |
| `GET` | `/api/docs/metadata` | `metadata.json` を返す |
| `POST` | `/api/chat` | `graph_query_agent` への単発質問（最終応答のみ返却） |
| `POST` | `/api/chat/stream` | `graph_query_agent` への質問（Server-Sent Events で逐次配信） |

---

## MCP サーバー

`mcp_server/` は CodeDoc のクエリエージェントを [Model Context Protocol](https://modelcontextprotocol.io) 経由で外部ホスト（Antigravity CLI、Claude Code、MCP Inspector など）に公開する FastMCP ベースのサーバーです。streamable-http トランスポートで動作し、ローカル起動と Cloud Run へのデプロイの両方をサポートします。

```bash
# ローカル起動 → http://127.0.0.1:8080/mcp
python -m mcp_server
```

`ask_codebase(question, session_id?)` ツールが、本リポジトリの **`graph_query_agent`**（Spanner コードグラフを用いるクエリエージェント。`adk run graph_query_agent` で単体起動するものと同一）を呼び出し、クラス関係・呼び出し階層・継承・影響範囲などの質問に回答します。`session_id` を渡すと同一スレッドで会話を継続できます。詳細は **[`mcp_server/README.md`](mcp_server/README.md)** を参照してください。

---

## パイプライン構成

```mermaid
graph LR
    subgraph Phase1["Phase 1: Scanner"]
        S1[scan_and_build_tree]
    end

    subgraph Phase15["Phase 1.5: Tree-sitter Entities"]
        TS[parse_entities]
    end

    subgraph Phase2["Phase 2: ファイル要約"]
        FS[file_summarizer]
    end

    subgraph Phase3["Phase 3: ディレクトリ要約"]
        DS[dir_summarizer]
    end

    subgraph Phase4["Phase 4: トピック抽出"]
        TE[topic_extractor]
    end

    subgraph Phase5["Phase 5: トピック要約"]
        TW[topic_writer]
    end

    subgraph Phase6["Phase 6: インデックス組立"]
        IW[index_writer]
    end

    subgraph Phase8["Phase 8: グラフノード"]
        GN[write_nodes]
    end

    subgraph Phase9["Phase 9: グラフエッジ"]
        GE[write_edges]
    end

    subgraph Phase10["Phase 10: エンベディング"]
        EM[generate_embeddings]
    end

    Phase1 --> Phase15 --> Phase2 --> Phase3 --> Phase4 --> Phase5 --> Phase6 --> Phase8 --> Phase9 --> Phase10
```

各フェーズの詳細仕様、レジューム機構、エラーハンドリング、リトライ戦略、パフォーマンス特性については以下を参照してください。

- **[`graph_generator/manual.md`](graph_generator/manual.md)** — 10 フェーズの詳細マニュアル
- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — パイプラインの全体アーキテクチャ

---

## ファイル構成

```
codedoc/
├── .env                          # Google Cloud 認証・モデル・Spanner 設定
├── .env.example                  # .env のテンプレート
├── requirements.txt              # Python 依存パッケージ（フルピン留め）
├── graph_generator/              # 中核のパイプライン実装（10 フェーズ）
│   ├── __main__.py               # CLI エントリーポイント
│   ├── pipeline.py               # 10 フェーズパイプライン本体
│   ├── treesitter_parser.py      # tree-sitter による PHP の AST エンティティ抽出（.ctp 含む）
│   ├── config.py                 # 設定（環境変数 + デフォルト）
│   ├── prompts.py                # Gemini 用プロンプト群
│   └── manual.md                 # 詳細マニュアル（日本語）
├── graph_query_agent/            # Spanner コードグラフに GQL を実行するクエリエージェント（graph-RAG）
│   └── agent.py
├── mcp_server/                   # MCP プロトコル経由で graph_query_agent を公開
│   ├── README.md
│   ├── server.py
│   └── deploy.sh                 # Cloud Run デプロイ
├── webapp/                       # FastAPI + Vite/React 19 のビューア兼チャット UI
│   ├── main.py                   # FastAPI 本体（DOCS_DIR は OUTPUT_DIR、既定 output_docs_pipeline/）
│   ├── requirements.txt
│   └── frontend/                 # React フロントエンド
│       ├── package.json
│       └── vite.config.js
├── setup_spanner_graph.py        # Spanner インスタンス + DB + テーブル + グラフを作成
├── output_docs_pipeline/         # 生成ドキュメントのデフォルト出力先
├── ARCHITECTURE.md               # （legacy）旧 code_doc_agent / vais_query_agent のアーキテクチャ
│
└── legacy/                       # （アーカイブ）旧実装の保管庫。詳細は legacy/README.md を参照
    ├── README.md
    ├── code_doc_agent/           # 旧 ADK ベース（6 フェーズ）のドキュメント生成エージェント
    ├── vais_query_agent/         # 旧 Vertex AI Search ベースのクエリエージェント
    ├── setup_vertex_search.py    # 旧 Vertex AI Search データストア作成スクリプト
    └── setup_vertex_search_gcs.py # 旧 GCS 経由のデータストア作成 + インポートスクリプト
```

---

## ライセンス

（プロジェクトのライセンスをここに記載してください）
