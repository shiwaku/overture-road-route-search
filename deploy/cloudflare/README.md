# 公開版（Cloudflare Workers ＋ Containers ＋ R2）

全球の到達圏・経路探索を、`shiwaku@mierune.co.jp` だけが使える形で公開する（issue #9）。
`../ksj-route-search-api` の `deploy/cloudflare` と同じ作りで、違いは API を大陸ごとに 3 台に分けたこと。

| 部分 | 置き場所 |
|---|---|
| 画面（SvelteKit の静的出力） | Workers の Static Assets |
| 道路タイル（3.7 GB） | R2（共有バケット `shi-works` の `pmtiles/overture-road-route-search/`） |
| API（FastAPI ＋ 探索グラフ） | Containers 3 台。地点の大陸の台だけが起きる |
| アクセス制限 | Cloudflare Access（`shiwaku@mierune.co.jp` だけ） |

## 3 台に分けた理由と大きさ

Containers は 1 台 12 GiB まで。全球のグラフは常駐 16 GiB なので、つながっている道路網（弱連結成分）ごとに分けた
（`scripts/16_split_world.py`）。別の道路網どうしはもともと経路がつながらないので、分けても結果は変わらない
（パリの 120 分の到達圏 77.9 万リンクが全球のグラフと一致）。

| 台 | 範囲 | リンク | 常駐（最大） | 読み込み | インスタンス |
|---|---|---|---|---|---|
| eurasia | ユーラシア・アフリカ（いちばん大きい道路網） | 4,218 万 | 9.6 GiB | 13.5 秒 | standard-4（12 GiB） |
| americas | 西経 25 度より西の道路網（北米・南米・ハワイなど） | 2,242 万 | 5.1 GiB | 6.6 秒 | standard-3（8 GiB） |
| other | 残り（日本・グレートブリテン島・オーストラリア・島々） | 963 万 | 2.3 GiB | 2.7 秒 | standard-1（4 GiB） |

常駐・読み込みは手元（Windows・SSD）の実測（`scripts/17_measure_shard.py`、2026-11-01）。速度は `legal`（制限速度が無い区間は法定速度）。

Worker は地点 → 台を `src/shard_grid.bin`（0.1 度のマス、`scripts/18_shard_grid.py`）で引く。
始点と終点が別の台の経路は、コンテナを起こさずに「別の道路網」と返す。
`/api/health` もコンテナを起こさずに Worker が答える（画面は開くたびにこれを待つので）。台を起こして確かめるときは `/api/health?shard=eurasia` など。

## 費用の見込み

起きている間だけ課金（最後のリクエストから `SLEEP_AFTER` = 10 分で寝る）。1 時間あたり（issue #9 の料金表から）:
standard-4 約 $0.11・standard-3 約 $0.075・standard-1 約 $0.038。Workers 有料プラン $5/月。R2 と Access（Free プラン・50 人まで）は無料枠。

| 1 か月に起きている時間 | 月額 |
|---|---|
| eurasia 20 時間・ほか各 10 時間 | 約 $8 |
| 各台 100 時間 | 約 $27 |

寝ている台が起きるときは、コンテナの準備とグラフの読み込みを待つ。Worker は読み込み中（503）なら 2 秒おきに最長 150 秒まで投げ直す。
本番での実測（2026-11-01、デプロイ直後の初回）:

| 台 | グラフの読み込み（手元） | 備考 |
|---|---|---|
| other | 19.8 秒（2.7 秒） | |
| americas | 24.4 秒（6.6 秒） | 2 回目の起動は約 75 秒 |
| eurasia | 34.9 秒（13.5 秒） | |

デプロイ直後の初回は、イメージの配布で数分かかり、150 秒を超えて 503 になった（もう一度検索すれば通る）。

## 準備（手元で作るもの）

```sh
uv run python scripts/16_split_world.py                       # network/world_<台>/（全球のグラフ network/world/graph が要る）
for s in eurasia americas other; do
  uv run python scripts/10_build_graph_arrays.py --case world_$s --speed legal --no-verify
done
uv run python scripts/18_shard_grid.py                        # deploy/cloudflare/src/shard_grid.bin
```

## 道路タイルを R2 に置く（初回・データ更新のとき）

3.7 GB あり、`wrangler r2 object put` の上限を超えるので、S3 互換 API で分割アップロードする（aws CLI。rclone でもよい）。
キーは `src/worker.ts` の `TILE_KEY`（`pmtiles/overture-road-route-search/roads_world_2026-10-21.0.pmtiles`）。

```sh
# プロファイル r2-shiworks（R2 の API トークン、region auto）を作っておく
aws s3 cp network/world/roads_world.pmtiles \
  s3://shi-works/pmtiles/overture-road-route-search/roads_world_2026-10-21.0.pmtiles \
  --profile r2-shiworks --endpoint-url https://<アカウント ID>.r2.cloudflarestorage.com
```

タイルの link_id は全球の番号。コンテナは台の link_id を `link_gid.npy` で全球の番号に直して返すので、タイルは全球のものをそのまま使う。

## デプロイ

`wrangler.jsonc` のコンテナは push 済みのイメージ（タグ付き）を指すので、ふだんのデプロイは Worker と画面だけで数十秒で終わる。
グラフを作り直したときは、先に下の「イメージの更新」をする。

```sh
cd deploy/cloudflare
npm install
npx wrangler login
npm run deploy      # 画面を build → Worker・Assets・コンテナ 3 台をデプロイ

curl https://overture-road-route-search.<アカウント>.workers.dev/api/health   # Access を付けたあとはブラウザで
```

`web/static/tiles/roads.pmtiles`（手元の画面用のハードリンク）は `web/static/.assetsignore` で Assets から外している。

### イメージの更新（グラフを作り直したとき）

Docker Desktop を動かし、リポジトリの直下で台ごとにビルドして push する（コンテキストは直下。中身は `.dockerignore` の許可リストで絞る）。
eurasia は約 10 GB あり、push に 1 時間近くかかることがある。途中で wrangler のログインが切れたら `npx wrangler login` してやり直す（push 済みの層は飛ばされる）。

```sh
TAG=$(git rev-parse --short HEAD)
for s in eurasia americas other; do
  docker build -f deploy/cloudflare/Dockerfile.$s -t overture-road-route-search-${s}container:$TAG .
  (cd deploy/cloudflare && npx wrangler containers push overture-road-route-search-${s}container:$TAG)
done
```

push したら `wrangler.jsonc` の `image` のタグを `$TAG` に書き換えて `npm run deploy`。
古いイメージ（2 回目のデプロイの残り `eurasiacontainer:7c2d2882` など）は `npx wrangler containers images list` で見て `images delete` で消す。

## アクセス制限（Cloudflare Access）

`shiwaku@mierune.co.jp` だけを通す。KSJ 版の README「社内限定の公開」と同じ手順で、ポリシーだけ違う。
Access は Cloudflare の手前で効くので、ログインしていない人のリクエストは Worker まで来ず、コンテナも起きない。

1. Workers & Pages → `overture-road-route-search` → **Access** タブ → **Protect this Worker behind Access**
2. **All traffic** を選ぶ（Previews only だと本番の URL は守られない）
3. **Authentication policy** で **Emails** に `shiwaku@mierune.co.jp` を入れて **Apply Access**
4. ログイン方法は **One-time PIN** だけにする（Zero Trust → ID プロバイダーに One-time PIN を足し、アプリケーションの「認証」で One-time PIN だけを選んで「インスタント認証を適用」）。
   「Protect this Worker behind Access」で作ったアプリは、One-time PIN にチェックが入っていないことがある。そのままだとログイン画面は出るが、コードのメールが送られない
   （アプリケーション一覧の「…」→ 編集 → ログイン方法で確かめる）

確認: シークレットウィンドウで開くとログイン画面になり、ほかのアドレスではコードが届かない。

スクリプトから API を呼ぶとき（ブラウザ以外）は、Access の Service Token をポリシーに「Service Auth」として足し、
`CF-Access-Client-Id`・`CF-Access-Client-Secret` ヘッダーを付ける。

## 一時停止

`wrangler.jsonc` の `workers_dev` を false にして `npx wrangler deploy`。Worker・イメージ・R2 のタイルは残るので、true に戻せば同じ URL で再開。
