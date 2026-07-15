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
8. [リポジトリ間の依存・結合度分析（`crossref`）](#8-リポジトリ間の依存結合度分析crossref)
9. [整合性の確認（`validate`）](#9-整合性の確認validate)
10. [既存グラフからの移行と注意点](#10-既存グラフからの移行と注意点)
11. [エンドツーエンドの例](#11-エンドツーエンドの例)
12. [FAQ・制約](#12-faq制約)

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

## 8. リポジトリ間の依存・結合度分析（`crossref`）

「独立した島」モデルでは、通常のグラフに**リポジトリをまたぐエッジは張られません**
（A→B の参照は `external`）。そのため「repo A を変更したら repo B に影響するか」
「repo A と B はどれくらい結合しているか」に答えるには、**明示的なクロスリポ依存レイヤ**を
別途構築します。これを行うのが `crossref` コマンドです（**全リポジトリ投入後に実行**）。

```bash
python -m graph_generator crossref            # クロスリポ依存を導出しグラフに書き込む
python -m graph_generator crossref --dry-run  # 書き込まず、結合度マトリクスだけ表示
```

`crossref` は各 repo の `entities.json`（投入時にコミット済み）から全 repo 横断の
FQCN レジストリを作り、**「repo B の中で、別 repo A に定義されたクラス／メソッド／関数を
参照している箇所」**を検出して、次のエッジを張ります:

- **CrossRepoRef**（Class→Class）: クラスレベルの参照。`kind` プロパティで参照の種類が分かります:

  | kind | 参照の形 |
  |---|---|
  | `import` | `use Shared\Money\Price;`（そのエイリアスを実際に使っているクラスに帰属） |
  | `class_ref` | `Price::class` リテラル / `'Shared\Money\Price::method'` callable 文字列 |
  | `extends` / `implements` / `uses` | 継承・インターフェース実装・trait 使用（最も強い結合） |
  | `new` | `new \Shared\Money\Price(…)` のインスタンス化 |
  | `type_hint` | 引数型・戻り値型（union/nullable も分解して判定） |
  | `instanceof` | `$x instanceof \Shared\Money\Price` |

- **CrossRepoFileRef**（File→Class）: **クラスを定義しないファイル**（bootstrap・routes・設定）
  からの参照。kind は CrossRepoRef と同じ。配線ファイルの依存もここで可視化されます。
- **CrossRepoCalls**（Method→Method）: レシーバ型が別 repo のクラスに特定できたメソッド呼び出し
  （型付き引数・コンストラクタ注入プロパティ・**戻り値型が宣言されたメソッド経由の連鎖レシーバ**
  `$sum = $base->add($tax); $sum->amount()` まで追跡）と、別 repo 定義の**名前空間関数**の呼び出し。
  型が特定できない動的呼び出しは対象外（部分カバレッジ）。クラス／import レベルは常に取れます。
- **DiBinds / DiInjects**（Class→Class）: DI コンテナ配線（CakePHP の `Application::services()` /
  `ServiceProvider`）から。`add(Interface::class, Concrete::class)` → **DiBinds**（インターフェース→具象）、
  `add(Service::class)->addArgument(Dep::class)` / `->addArguments([A::class, B::class])` →
  **DiInjects**（サービス→依存）。両端が別々の他 repo にあるペア（配線だけ第三の repo にある場合）も
  対象。`::class` ベースなので静的・決定的。文字列キー（`add('key', …)`）やクロージャ工場は
  対象外（実行時のため）。

**誤検出ゼロの解決規則（PHP の名前解決に準拠）:**

- クラス名は PHP のとおり**ただ一つの FQCN に解決**されます。`namespace App;` 内の `Widget::class` は
  常に `App\Widget` であり、グローバルや他 repo の `Widget` に**フォールバックしません**
  （callable 文字列は常に完全修飾、関数だけが「現在の名前空間 → グローバル」の順でフォールバック）。
- **vendor ミラーは所有者にならない**: `--include-vendor` で取り込んだ repo の `vendor/` 内に
  共有ライブラリのコピーがあっても、そのコピーはシンボルを所有せず、参照元にもなりません。
  参照は共有ライブラリの**ソース repo** に張られます（欲しい結合はまさにこれ）。
- **参照元 repo 自身のコミット済み定義が勝つ**: repo 内に同じ FQCN の（vendor でない）定義が
  あれば cross エッジは張られません（composer の実行時挙動と一致。件数はレポートに出ます）。
- **複数の他 repo が同じ FQCN を定義**していたら曖昧 — エッジは張らず、**必ずレポート**されます。

**捨てたものは必ず見える**: エッジにしなかった参照（曖昧 FQCN とその定義 repo 一覧・
未所有参照の上位名前空間・ローカル定義優先の件数・DI の未所有/曖昧）は、実行時に
「dropped / unresolved」セクションとして表示され、`OUTPUT_DIR/crossref_report.json` にも
書き出されます（CI で差分監視できます）。

### DI 影響分析・結合度

コンストラクタ注入や、インターフェース→具象の束縛が別 repo にまたがる場合、それは DiInjects /
DiBinds として現れます。例：

```gql
-- この共有ロガーを変更したら、DI で注入している側（サービス）はどこか
GRAPH code_graph_a
MATCH (s:Classes)-[i:DiInjects]->(d:Classes)
WHERE d.fqcn = 'Shared\Logging\Logger'
RETURN s.repo, s.fqcn

-- 共有インターフェースを実装している具象クラス（束縛）はどこか
GRAPH code_graph_a
MATCH (iface:Classes)-[b:DiBinds]->(impl:Classes)
WHERE iface.fqcn = 'Shared\Contracts\Clock'
RETURN impl.repo, impl.fqcn
```

結合度マトリクスは `refs / calls / di`（di = DiBinds + DiInjects の本数）で表示されます。

### 影響分析（Impact）

「`Shared\Money\Price` を変更したら、他 repo のどこが影響を受けるか」:

```gql
GRAPH code_graph_a
MATCH (src:Classes)-[r:CrossRepoRef]->(tgt:Classes)
WHERE tgt.fqcn = 'Shared\Money\Price'
RETURN DISTINCT src.repo, src.fqcn, r.kind
```

メソッド単位（`Price::add` の変更で壊れうる呼び出し元）:

```gql
GRAPH code_graph_a
MATCH (caller:Methods)-[c:CrossRepoCalls]->(callee:Methods)
WHERE callee.fqmn = 'Shared\Money\Price::add'
RETURN caller.repo, caller.fqmn
```

### 結合度（Coupling）

repo ペアごとの参照本数（多いほど密結合）:

```gql
GRAPH code_graph_a
MATCH (s:Classes)-[r:CrossRepoRef]->(t:Classes)
RETURN r.source_repo, r.target_repo, COUNT(*) AS coupling
ORDER BY coupling DESC
```

`crossref` 実行時と `validate` 実行時には、この結合度マトリクスがコンソールにも表示されます:

```
  [crossref] coupling matrix:
    source_repo → target_repo :  refs / calls / di
      web → shared :  12 / 34 / 3
      api → shared :  8 / 20
```

いずれかの repo を再投入したら `crossref` を再実行してください（決定的な edge_id なので
再実行は冪等です）。

## 9. 整合性の確認（`validate`）

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

## 10. 既存グラフからの移行と注意点

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

## 11. エンドツーエンドの例

```bash
# 0) 設定（.env）— 全リポジトリで同じ Spanner インスタンス/DB/GRAPH_NAME を共有
python -m graph_generator init

# 1) Spanner リソース作成（既存なら不足分だけ冪等に追加）
python -m graph_generator setup spanner

# 2) 複数リポジトリを 1 グラフへ投入（逐次 or 一括のどちらでも）
python -m graph_generator analyze ./web --repo-name web
python -m graph_generator analyze ./api --repo-name api
#   または: python -m graph_generator analyze --repos repos.json

# 3) リポジトリ間の依存・結合度を導出（全 repo 投入後に実行）
python -m graph_generator crossref

# 4) 整合性チェック（repo 別件数・結合度マトリクス・孤立エッジ 0 を確認）
python -m graph_generator validate

# 4) クエリ（ADK REPL / MCP / Web UI いずれか）
adk run graph_query_agent
```

グラフを Spanner に書き込む前に、GCP 不要でローカルにグラフ内容を検証したい場合は
`python -m graph_generator evaluate`（フィクスチャに対する精度評価）を利用できます。

## 12. FAQ・制約

- **Q. リポジトリ間で本当に同名クラス/メソッドが混ざらない?**
  A. 混ざりません。ノード ID が `repo` を含むため別ノードになります。これは
  `tests/test_multi_repo.py` で「同一ソースを 2 つの repo として投入してもノード ID 集合が
  完全に素（disjoint）」「どのエッジも repo 境界を越えない」ことを検証済みです。

- **Q. 別リポジトリの共有ライブラリを呼んでいる。依存は見える?**
  A. 通常のグラフ（MethodCalls 等）には**張られません**（`external` として集計のみ、独立した島）。
  ただし `crossref` コマンドを実行すると、専用の **CrossRepoRef / CrossRepoFileRef /
  CrossRepoCalls** エッジとしてクロスリポ依存が張られ、影響分析・結合度クエリに使えます
  （[8章](#8-リポジトリ間の依存結合度分析crossref)）。
  通常の同一リポ内クエリは島のまま汚れません。

- **同名ディレクトリのリポジトリ**: `--repo-name` を省略するとベース名が repo 名になるため、
  異なるパスでもディレクトリ名が同じだと同一 repo とみなされ統合されます。**必ず
  `--repo-name` を明示**してください。`validate` の repo 別件数で取り違えに気付けます。

- **vendor**: vendor はリポジトリごとに既定で除外されます。取り込みは各リポジトリ単位で
  フラグ / `.env` / マニフェストの `include_vendor` で制御します。

- **ドキュメントビューア（webapp）**: 現状は単一 `OUTPUT_DIR` を配信する作りのため、複数
  リポジトリのブラウズ UI は本ガイドの対象外です（グラフ + クエリ層が対象）。
