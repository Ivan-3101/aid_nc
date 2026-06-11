-- FIX: Tighten the rulesDev prompt to enforce single-line valid JSON output.
--
-- Problem:
--   The LLM generates JSONLogic that spans multiple lines with literal newline
--   characters inside the "answer" string value.  json.loads() then raises:
--     JSONDecodeError: Invalid control character at: line 1 column N
--
-- Why this matters:
--   A JSON string value MUST use the escape sequence \n (backslash-n) to
--   represent a newline.  Emitting a raw 0x0a byte inside a string literal
--   is invalid per RFC 8259 §7.
--
-- Fix:
--   The prompt is updated to explicitly instruct the model to:
--     1. Output JSON on a single line (no top-level whitespace breaks)
--     2. Escape any newlines within the answer value as \n
--     3. Not wrap the output in markdown code fences
--
-- The app-level safe_json_loads() wrapper handles this transparently for
-- agents that are not yet updated, but fixing the prompt is the correct
-- long-term solution.
--
-- Run this script once, then POST /reloadconfig.

UPDATE masters.sysconfig
SET config = jsonb_set(
    config,
    '{prompt_template, template}',
    to_jsonb(
        'You are a rulesDev agent. You will help develop new rules in Drona. '
        'Below is the policy document that you have to follow - {policy}. '
        'Following is the user''s rule expectation; review it step by step '
        'following the policy - {query}. '
        'From historical rule requirements, here are the most relevant '
        'examples - {similar_cases}. '
        'Please construct JSONLogic for the rule. '
        'Output your response as a single-line JSON object with NO markdown, '
        'NO code fences, and NO literal newline characters inside string values '
        '(use the escape sequence \n instead). '
        'Format: {"answer": "<complete JSONLogic as a compact single-line string>"}'
    )
)
WHERE cfgname = 'agents'
  AND config @> '[{"agent": "rulesDev"}]';

-- Verify
SELECT config->'prompt_template'->>'template' AS template
FROM   masters.sysconfig
WHERE  cfgname = 'agents'
  AND  config @> '[{"agent": "rulesDev"}]';
