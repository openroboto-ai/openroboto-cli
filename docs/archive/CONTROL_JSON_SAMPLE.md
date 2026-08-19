> # ⚠️ 已归档 —— 内容已并入 `../control_json.md`
>
> **状态**：superseded · **归档日期**：2026-08-19
> **替代者**：`docs/control_json.md`（契约说明 + 字段表 + 示例三合一）
>
> **为什么**：control.json 一件事原本摊在三个文件里 —— 契约说明
> （`control_json.md`）、示例讲解（本文件）、示例本体
> （`control_json_example.json`）。本文件全文只有 15 行，且只是在说
> 「示例在隔壁那个 json 里」。

---

# control.json Sample

The canonical placeholder sample is `control_json_example.json`.

It contains only miner-visible and validator-visible fields:

- round number, status, and public message;
- evaluation-fee parameters;
- public training and validation resource URLs;
- public model and training parameters;
- an optional public read credential.

The sample intentionally omits scoring-service settings, held-out task data, emission controls, owner process controls, internal URLs, wallet identifiers, and write credentials.

Clients should treat the fetched document as untrusted input: require expected types, reject invalid fee or round values, use HTTPS for remote resources, and ignore unknown fields.
