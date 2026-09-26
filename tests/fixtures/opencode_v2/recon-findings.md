# OpenCode v2 recon findings

- **credential_seed_status**: 204
- **2_prompt_resume_false_records_without_running**: yes
- **permission_reply_status**: 204
- **form_reply_status**: 204
- **permissions_answered**: 3
- **forms_answered**: 1
- **terminal_event**: session.execution.succeeded
- **compact_status**: 200
- **compaction_terminal_event**: session.compaction.ended
- **6_session_list_format_json**: yes
- **1_instructions_applies_system_prompt**: no
- **3_tool_progress_has_incremental_output**: no (metadata keys seen: shellID, toolCalls)
- **4_codemode_false_mcp_raises_permission_asked**: no
- **5_safety_action_names**: opencode_list_mcp_resources, question, shell

## Notes from the capture runs
- Item 4 read "no" in this capture because the model reached the MCP server through Code Mode (`opencode_list_mcp_resources`). An earlier run on the same server config captured `permission.asked` with `action: recon-echo_echo` (MCP tool = `<server>_<tool>`), so MCP calls do raise permission asks; Code Mode remains active for builtin tool discovery even with `codemode: false` on the server entry.
- Captured with deepinfra/Qwen/Qwen3.8-Max; OpenCode Go (glm-5.3) produced the same event families in a prior run.
