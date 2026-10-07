# vpipe-api

[![ci](https://github.com/kabatin/vpipe-api/actions/workflows/ci.yml/badge.svg)](https://github.com/kabatin/vpipe-api/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![macOS 26+ · Apple Silicon](https://img.shields.io/badge/macOS_26%2B-Apple_Silicon-lightgrey.svg)

**[vpipe](https://github.com/tgo-app-dev/vpipe) の生成パイプラインを、Apple Silicon Mac 上の HTTP ジョブ API として公開する。**

[English](README.md) | 日本語

vpipe は MiniMax H3（動画）や Qwen-Image、FLUX などの大型生成モデルを、独自の Metal カーネルで端末内だけで動かす。
`vpipe-api` はその CLI を小さく安全なジョブサーバーで包み、ほかのツールから仕事を投げて結果を受け取れるようにする。
想定する呼び出し元は、編集ツール、書き出し用のスクリプト、同じ LAN 上の Web アプリなど。

```
POST /v1/workflows/minimax-h3-turbo-video/jobs   → 202 {id}
GET  /v1/jobs/{id}                               → queued → running（進捗）→ succeeded
GET  /v1/jobs/{id}/output                        → video/mp4
```

- **ワークフロー登録式**
  - 呼び出し側が選べるのは、名前付きで検証済みの手順（`minimax-h3-turbo-video`）だけ
  - 生のパイプライン JSON は受け付けない。LAN に公開しても、任意のファイルを読み書きさせられることはない
- **GPU は 1 枠。混んでいるときは正直に断る**
  - ジョブは 1 件ずつ処理する
  - 実行中の 1 件と、待ち行列（既定 1 件）が埋まっていたら、`429 busy` と `Retry-After` を返す
  - 何時間分もの仕事を黙って溜め込まない
- **失敗を本当に検知する**
  - vpipe は、実行中にステージが失敗しても exit 0 で終わる
  - そこで vpipe-api は、ログの失敗行と「出力ファイルが今回書かれたか」を確かめてから成功とする
- **再起動に強い**
  - ジョブはディスクに保存する
  - 再起動したら、待ち行列にあったものは再開し、実行中だったものは「再試行できる失敗」として報告する
- **`doctor` と `setup`**
  - マシンの点検、vpipe のビルド、モデルの取得と準備ができる
  - ダウンロードには見張りが付いていて、止まったら自動で再開する

## ワークフロー：`minimax-h3-turbo-video`

[MiniMax H3](https://huggingface.co/MiniMaxAI/MiniMax-H3)（FL2VA、8bit）に、コミュニティ製の
[Turbo LoRA](https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora) を組み合わせる。
- テキストから動画を作る。開始フレームと終了フレームを画像で指定することもできる
- 現実的な時間で終わるサイズで生成し、指定した解像度ちょうどに拡大する（lanczos、はみ出す分は中央で切り抜き）
- 音声は捨てる

| | |
|---|---|
| 出力 | 64〜4096px の任意サイズ、比率は 16:9〜9:16。H.264 MP4（limited range の BT.709）、24fps、音声なし。vpipe の中間ファイルは可逆なので、劣化するのはこの最後の圧縮だけ |
| 長さ | `frames` は 17n+5：56（2.33 秒）〜243（10.125 秒） |
| 画質段 | `draft`（16:9 なら 832×480）・`standard`（1024×576）・`final`（1344×768。H3 の学習時の解像度） |
| 開始・終了フレーム | `start_image`・`end_image`（base64 の PNG/JPEG/WebP、20MB 以下。終了フレームには開始フレームが必須） |

実測（M5 MacBook Pro、GPU 10 コア、32GB、6 ステップ）

| | 124 フレーム（5.2 秒） | 243 フレーム（10.1 秒） |
|---|---|---|
| `draft` | 約 8 分 | 約 17 分 |
| `standard` | 約 10.5 分 | 約 24 分 |
| `final` | 約 22 分 | 約 54 分 |

> **ライセンスの注意**
> - MiniMax H3 の重みは *MiniMax H3 Community License* に従う
> - 米国・EU・英国・韓国では利用できない。商用利用にも独自の条件がある
> - 取得するすべてのモデルについて、出力を使う前にライセンスを確認すること
> - vpipe-api 自体は Apache-2.0 で、重みは一切再配布しない

## 必要なもの

- Apple Silicon Mac、macOS 26 以降
- ディスク：H3 で約 65GB（準備中は最大 185GB）
- Python 3.12 以降と [uv](https://docs.astral.sh/uv/)、`ffmpeg`/`ffprobe`、Xcode（vpipe のビルド用）、`cmake`
- curl の例を試すなら `jq`
- メモリは 16GB でも動く。多いほど重みの読み直しが減って速くなる（いちばん食うのは `final`。[運用上の注意](#運用上の注意)を参照）

## 導入

```sh
uv tool install git+https://github.com/kabatin/vpipe-api
```

### 1. vpipe（ビルド済みなら省略）

```sh
vpipe-api setup vpipe --dir ~/vpipe/src          # v0.1.80 を clone して cmake でビルド（約 20 分）
```

最後に、`~/.config/vpipe-api/config.toml` に書く 2 行が表示される：

```toml
vpipe_bin = "/Users/you/vpipe/src/build/apps/vpipe/vpipe"
work_dir  = "/Users/you/vpipe/work"              # モデルとその登録簿はここに置かれる
```

### 2. モデル

```sh
vpipe-api setup models minimax-h3-turbo-video     # 約 118GB を取得し、8bit に量子化する
```

- ダウンロードは、止まったところから再開できる
- モデルのフォルダが 10 分増えないと、取得を自動で再起動する
- 例：ネットワークを切り替えると、vpipe の通信が古い IP に張り付いたまま止まることがある。これも自動で立ち直る

### 3. 点検と起動

```sh
vpipe-api doctor --smoke     # 環境の点検と、ごく短い生成を 1 本
vpipe-api serve              # http://127.0.0.1:8765（仕様は /docs）
```

### 4. 最初のジョブ

```sh
API=http://127.0.0.1:8765        # トークンを設定したら、各 curl に -H "Authorization: Bearer $TOKEN" を足す
JOB=$(curl -s -X POST "$API/v1/workflows/minimax-h3-turbo-video/jobs" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: first-job' \
  -d '{"prompt": "A small wooden boat drifting on a calm lake at dawn.",
       "output": {"width": 1280, "height": 720}, "quality": "draft"}' | jq -r .id)
curl -s "$API/v1/jobs/$JOB" | jq '{status, progress}'     # "succeeded" になるまで繰り返す
curl -s -o clip.mp4 "$API/v1/jobs/$JOB/output"
```

開始・終了フレームの指定、取り消し、`429 busy` の扱いは [examples/curl.md](examples/curl.md) にある。

### 更新

```sh
uv tool upgrade vpipe-api    # main の最新を入れる
```

そのあと `vpipe-api serve` を再起動する。先に `/v1/health` が `"running": 0` になるのを待つこと：
- 再起動のときに実行中だったジョブは、再試行できる失敗（`server_restarted`）で終わる
- 待ち行列のジョブは、再起動後にそのまま続く

## 設定

`~/.config/vpipe-api/config.toml`（または `$VPIPE_API_CONFIG`）に書く。単一の値は `VPIPE_API_<名前>` の環境変数でも指定できる。

| キー | 既定値 | |
|---|---|---|
| `vpipe_bin`, `work_dir` | — | serve に必須 |
| `vpipe_src_dir` | `vpipe_bin` をビルドしたフォルダ | vpipe のソース。`setup models` がここのパイプライン定義を読む |
| `host` / `port` | `127.0.0.1` / `8765` | ループバック以外で待ち受けるなら `token` が**必須** |
| `token` | — | 設定すると、すべてのリクエストに `Authorization: Bearer <token>` が必要 |
| `max_waiting` | `1` | 実行中のジョブの後ろで待てる件数 |
| `retention_days` | `7` | 終わったジョブと出力は、この日数が過ぎたら消える |
| `data_dir` | `~/.local/share/vpipe-api` | ジョブの記録と出力を置く場所 |
| `job_timeout_factor` | `3.0` | 「見積もり × この値 ＋ 5 分」を過ぎたジョブは止める |
| `max_body_mb` | `64` | リクエストの大きさの上限 |
| `ffmpeg` / `ffprobe` | `ffmpeg` / `ffprobe` | 後処理に使うコマンド。`PATH` から探す |

ワークフローごとの設定：

```toml
[workflows."minimax-h3-turbo-video"]
sol_attn = false          # 高速な近似をやめて正確な attention に。全ジョブに効く（遅くなる）
i8_gemm  = true           # M5 以降の matrix core を使う
lora     = "larryvrh/MiniMax-H3-Turbo-Lora-v4-600-ema"
```

### LAN に公開する

```sh
export VPIPE_API_HOST=0.0.0.0
export VPIPE_API_TOKEN="$(openssl rand -hex 32)"
vpipe-api serve
```

- トークンが無いと、ループバック以外では起動しない。トークンは印字可能な ASCII 文字で 32 文字以上
- 設定ファイルは他人に読ませない：`chmod 600 ~/.config/vpipe-api/config.toml`（`doctor` が警告する）
- TLS は無く、トークンは平文で流れる。信頼できるネットワークで使うか、TLS のリバースプロキシの後ろに置く
- **プロキシ経由で公開するときもトークンは外さない**
  - トークンなしのサーバーは、プロキシを経由したリクエストを拒否する
  - `Host` がループバック以外のリクエストも拒否する。DNS リバインディングを使う Web ページからの攻撃を防ぐため
- トークンを設定すると `/docs` にもヘッダーが要る。見るときは、トークンなしで手元に立てたサーバーを使うか、curl で `/openapi.json` を読む
- ログイン時に自動で起動するなら、launchd の例 `examples/com.github.kabatin.vpipe-api.plist` を使う

## API

[docs/api.md](docs/api.md) と [examples/curl.md](examples/curl.md) を参照。OpenAPI は `/openapi.json` で取得できる。
投入のたびに `Idempotency-Key` を送ること。タイムアウト後に再送しても、二重に生成せず同じジョブが返る。

## ワークフローの追加

1. `vpipe_api.workflows.base.Workflow` を継承する
   - 宣言するもの：`id`、`params_model`（pydantic。そのまま JSON Schema と型付きの POST ルートになる）、`required_models`
   - 実装するもの：`store_inputs`、`estimate_seconds`、`prepare`（vpipe のパイプライン JSON と生の出力先を返す）、`finalize`、`smoke_params`
2. `vpipe_api/workflows/__init__.py` の `build_registry` に登録する
3. `tests/conftest.py` の偽 vpipe を使ってテストを書く

ワークフローは閉じたままにする。呼び出し側に選ばせるのはパラメータだけで、ファイルパスやステージの構成は選ばせない。

## 運用上の注意

- 重い処理は 1 つずつ。Metal のメモリは固定で確保されるので、生成中に大きな GPU アプリ（書き出し、ローカル LLM など）を動かすと、両方が遅くなるかメモリが尽きる
- いちばん重いのは `final` × 243 フレーム。32GB の M5 では、空きメモリが最小 18% まで減り、スワップは 9.7GB から 14.8GB に増えた（生成は問題なく完了）。16GB の Mac では、長い `final` に頼る前に短い `final` で試すこと
- 長いバッチは電源につないで回す。バッテリーだと性能が落ち、すぐに減る
- ファンの無い Mac は、長いクリップで熱のため遅くなる。上の表より時間がかかる前提で

## 開発

```sh
uv sync
uv run pytest            # カバレッジ 80% 以上が条件。偽の vpipe を使う。一部のテストは ffmpeg が必要
uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright
```

## ライセンス

Apache-2.0。vpipe は作者の Apache-2.0 に、モデルの重みはそれぞれのライセンスに従う。
