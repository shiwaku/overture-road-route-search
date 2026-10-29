# Overture 道路データを経路探索に使える形にする — 進め方

2026-10-28 作成。

## 目的

Overture Maps の道路データ（transportation の `segment` / `connector`）を、日本全域で経路探索と到達圏分析に使えるようにする。
あわせて、次の2つを確かめる。

- 道路データの網羅率（国の統計・国土数値情報との比較）
- 経路探索エンジンの間で結果が一致するか

参考にするもの:

- [mbasa/overture-to-pgr](https://github.com/mbasa/overture-to-pgr)（Overture → pgRouting の PL/pgSQL。ジオロニアのマリオさん作）
- 姉妹プロジェクト `../ksj-route-search-api`（国土数値情報 N13 版。scipy in-memory を採用し、pgRouting と OSRM・GraphHopper・Valhalla を比較済み）

## 決めたこと

| 項目 | 決定 |
|---|---|
| 対象範囲 | まず日本全域で検証し、のちに全球へ広げる（2026-10-29 決定。スクリプトは範囲を引数で変えられるように作り、日本に固有の決めごとは分けておく）。日本全域は（日本を囲む四角形で取ったあと、Overture `divisions` の日本の境界で切り抜く。四角形には韓国の南東部・サハリン・千島列島も入るため） |
| Overture の版 | `2026-10-21.0`（最新版で固定。途中で変えない） |
| 移動手段 | 車・徒歩・自転車の 3 つ（それぞれ別のネットワークにする） |
| 網羅率の比較先 | 道路統計年報（国交省）＋国土数値情報 N13 |
| エンジン | 5 つ（pgRouting・SciPy・OSRM・GraphHopper・Valhalla）とも全国で前処理を試す。同じ経路になるかの比較は狭い範囲で行う（東京 23 区など・KSJ 版と同じ） |
| 管理 | git。GitHub の非公開リポジトリ shiwaku/overture-route-search（2026-10-29 作成。最終的に公開する予定） |

## 日本範囲の件数（2026-10-21.0・四角形 東経 122〜154°・北緯 20〜46°・切り抜き前）

| | 本数 |
|---|---:|
| 道路（subtype=road）合計 | 約 1,650 万 |
| うち車が通れる種類（motorway〜living_street・service・unclassified） | 約 1,360 万 |
| うち徒歩・自転車向け（footway・path・steps・cycleway・pedestrian・bridleway） | 約 185 万 |
| track（農道・林道など） | 約 101 万 |

全球（2026-10-21.0）は segment 3.53 億件（72 GB）・connector 4.24 億件（25 GB）。日本の四角形の約 21 倍。

全国では GeoJSON → `ogr2ogr`（overture-to-pgr の手順）は重すぎるので、DuckDB で GeoParquet を直接取ってくる。

## 段階

| # | 段階 | やること | 結果として出すもの |
|---|---|---|---|
| 0 | 準備 | uv で Python プロジェクトを作る（DuckDB の Python 版）。Docker で PostGIS と pgRouting を立てる | 作業する環境 |
| 1 | 取得 | `segment` / `connector` / 日本の境界を取得し、日本の範囲で切り抜いて GeoParquet に保存 | 生データ |
| 2 | 網羅率の検証 | 下の「網羅率」 | 比較表、都道府県別・メッシュ別の地図 |
| 3 | ネットワーク化 | つなぎ点で区間を切り分ける。交通手段ごとに通れるかを判定（`access_restrictions` の mode）。一方通行と制限速度から順方向・逆方向のコストを作る。道路網の分断を調べる | 共通の中間形式（リンクとノードの parquet）を、車・徒歩・自転車の 3 つ |
| 4 | 各エンジン用に変換 | pgRouting（overture-to-pgr をそのまま動かした版と、改良した版）、SciPy（方向ごとの重みを持つ CSR）、OSM PBF（OSRM・GraphHopper・Valhalla 用・oneway 付き） | エンジンごとの入力データ |
| 5 | 経路の検証 | エンジンの間で同じ経路になるか（KSJ 版の `bench/compare` を流用）。KSJ 版との比較（同じ 2 地点・同じ出発地点）。pgRouting がどの規模まで実用か | 検証の記録 |
| 6 | まとめ | 割り切ったこと、KSJ 版との違い、どちらを使うべきか | レポート |

## 網羅率

- **道路統計年報**: 道路の種類別・都道府県別の延長と比べる。Overture の class は OSM の機能による分類で、道路法の分類とは一致しないので、合計の延長と、おおまかな対応づけでの比較になる。統計の対象外の道（service・track など）を含めた場合と除いた場合の両方を出す
- **国土数値情報 N13**: 重ね合わせて、N13 の道路分類（`N13_003`）ごと・メッシュごとに「Overture にある割合」を出す。手元の `../ksj-route-search-api/input/geoparquet/` を使う
- **属性の埋まり具合**: 制限速度・一方通行・道路名・幅員・舗装

## まだ決めていないこと

| # | 項目 | 状況 |
|---|---|---|
| ① | 道路統計年報の版（どの年の値と比べるか） | **決定**: 道路統計年報 2025（2024-03-31 時点）。結果は `docs/coverage.md` |
| ② | 制限速度が無い区間の速度 | KSJ 版の表（高速 80 / 国道 35 / 都道府県道 30 / 市区町村道 20 km/h）に合わせるか、Overture の class ごとに決めるか。KSJ 版と比べやすいのは前者 |
| ③ | 徒歩・自転車の速度 | 案: 徒歩 4.8 km/h（KSJ 版の前身は 3.6 km/h）、自転車 15 km/h |
| ④ | 日本の境界の扱い | **決定（2026-10-28）**: 係争地（XA 歯舞・XB 竹島・XD 尖閣・XI 択捉・XO 色丹・XU 国後）は `country` 列で区別して残す。網羅率の比較では外し、ネットワークには残す（本土と道路でつながらないので経路には影響しない） |
| ⑤ | pgRouting の版 | KSJ 版では 3.6 以降の `pgr_drivingDistance` が全国で 6〜7 分かかった。3.5 系（kartoza/postgis:15-3）と最新版の両方を用意するか |
