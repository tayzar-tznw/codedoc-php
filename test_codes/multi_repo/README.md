# multi_repo フィクスチャ — クロスリポジトリ依存の検証用

`crossref`（リポジトリ間の依存・結合度分析）を検証するための、**相互依存する 3 つの小さな
PHP リポジトリ**です。フレームワーク非依存（namespace 付きの素の PHP）なので composer/vendor
不要でそのまま解析できます。

- 機能の説明は [../../MULTI_REPO_GUIDE.md](../../MULTI_REPO_GUIDE.md) を参照。
- 期待値（正解データ）は [`cross_ground_truth.json`](cross_ground_truth.json)。
- 自動検証は `tests/test_multi_repo_fixture.py`（`crossref` 導出を正解データと完全一致で
  突き合わせる）と `tests/test_crossref_accuracy.py`（名前解決・所有権ルールの回帰テスト）。

## 構成（「共有ライブラリを 2 サービスが使う」構図）

| repo | namespace | 役割 |
|---|---|---|
| `shared` | `Shared\` | 共有ドメインライブラリ（`Price` / `Logger` / `Clock` + 名前空間関数 `Shared\Util\money_round`） |
| `web` | `Web\` | `shared` に依存するサービス（クラスなしの `bootstrap.php` を含む） |
| `api` | `Api\` | `shared` に依存するサービス |

```
web ─┐
     ├─▶ shared     （web と api がともに shared に依存 = shared への fan-in）
api ─┘
```

## 何を検証するか

**捕捉される依存（正例）**
- **クラスレベル（CrossRepoRef）** — kind ごとに 1 パターン以上:
  - `import`: `use Shared\...`（そのエイリアスを使うクラスに帰属）
  - `class_ref`: `Price::class` / `services()` 内の `\Shared\...::class`
  - `implements`: `SystemClock`（エイリアス経由）と `FrozenClock`（**インライン FQCN、
    import なし**）の両方が `Shared\Contracts\Clock` を実装
  - `new` / `type_hint` / `instanceof`: `Web\Money\Cart` がインライン FQCN だけで
    `Shared\Money\Price` を生成・返却型宣言・instanceof 判定
- **ファイルレベル（CrossRepoFileRef）**: クラスを定義しない `web/bootstrap.php` の
  `use` / `new` / 戻り値型が File→Class エッジとして捕捉される。
- **メソッド呼び出しレベル（CrossRepoCalls）**: レシーバ型が静的に分かる呼び出し。
  - 型付き引数レシーバ（`Price $base` → `$base->add()`）
  - **コンストラクタ注入（DI）プロパティ**（`__construct(private Logger $logger)` → `$this->logger->info()`）
  - **連鎖レシーバ**: `$sum = $base->add($tax); $sum->amount()` — `add()` の宣言戻り値型
    `Price` から `$sum` の型を復元して `Price::amount` を捕捉
  - **名前空間関数**: `web/bootstrap.php` の `web_boot()` が `\Shared\Util\money_round()` を呼ぶ
    （両端とも `(global)` 擬似クラスのメソッドノード）
- **DI コンテナ配線（DiBinds / DiInjects）**: `web/src/Application.php` の `services()` が
  `add(OrderService::class)->addArgument(Logger::class)` で web→shared の注入（DiInjects）を、
  `add(Clock::class, SystemClock::class)` で shared インターフェース→web 具象の束縛（DiBinds）を張る。
- **結合度マトリクス**: `web→shared`（refs 16 = クラス 13 + ファイル 3 / calls 4 / di 1）、
  `shared→web`（di 1）、`api→shared`（refs 4 / calls 2）。

**捕捉されない境界（負例・仕様として明記）**
- **同名クラスの誤結合なし**: `Web\Support\Logger` は `Shared\Logging\Logger` と単純名が同じだが
  別クラス。`Web\Support\LocalReporter` はローカルの方を使うので、shared への誤エッジは張られない
  （PHP のクラス名解決は他の名前空間へフォールバックしない）。
- **動的レシーバ**: `DynamicDispatcher::run` の `$handler->handle()` は実行時型のため対象外。
- **未所有型はレポート行き**: `Web\Application` の `Cake\Core\ContainerInterface` 型ヒントは
  どの repo も所有しないため、エッジにはならず `drops.unowned_sample` に必ず現れる。
- **戻り値型宣言のない連鎖**: 連鎖レシーバの型復元は**宣言された戻り値型**経由のみ
  （`Price::add(): Price` があるから `amount` が捕捉できる）。宣言のないメソッドを挟む連鎖は
  捕捉されない（正直な残存境界）。

## ルール（他フィクスチャと同様）

- 正解データはコードの実挙動を固定したもの。**コードを変えたら `cross_ground_truth.json` も同じ変更で更新**し、
  `.venv/bin/python -m pytest tests/test_multi_repo_fixture.py` を通すこと。
- 同名衝突・DI・動的・連鎖・インライン FQCN・クラスなしファイルの各パターンが**検証の本体**。
  安易に単純化・改名しないこと。
