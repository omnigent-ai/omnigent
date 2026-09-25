#!/usr/bin/env node
/** Require explicit visibility on JavaScript/TypeScript session-list requests. */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(
  new URL("../../web/package.json", import.meta.url),
);
const ts = require("typescript");
const UNKNOWN = "\u0000";
const RULE = "session-list-visibility";
const text = (value = UNKNOWN) => ({ strings: [value] });
const strings = (value) => value?.strings ?? [UNKNOWN];
const choices = (values) => ({ strings: values.flatMap(strings).slice(0, 32) });
const nameOf = (node) =>
  node && (ts.isIdentifier(node) || ts.isStringLiteralLike(node))
    ? node.text
    : undefined;
const functionOf = (node) => {
  while (node && !ts.isFunctionLike(node) && !ts.isSourceFile(node))
    node = node.parent;
  return node;
};

function updateQueries(queries, keys, values, operation = "set") {
  const result = [];
  for (const query of queries) {
    for (const key of keys) {
      for (const value of values) {
        const params = new URLSearchParams(query);
        if (operation === "delete") params.delete(key);
        else params.set(key, value);
        result.push(params.toString());
        // An unexamined branch cannot establish visibility.
        if (result.length > 32) return [...result.slice(0, 31), ""];
      }
    }
  }
  return result;
}

export function analyzeSource(filename, source) {
  const file = ts.createSourceFile(
    filename,
    source,
    ts.ScriptTarget.Latest,
    true,
  );
  if (file.parseDiagnostics.length) {
    const diagnostic = file.parseDiagnostics[0];
    const line =
      file.getLineAndCharacterOfPosition(diagnostic.start ?? 0).line + 1;
    throw new Error(
      `${filename}:${line}: ${ts.flattenDiagnosticMessageText(diagnostic.messageText, " ")}`,
    );
  }
  const scopes = new Map();
  const writes = [];
  const calls = [];
  const comments = new Map();
  function index(node, parentScope) {
    if (ts.isFunctionDeclaration(node) && node.name)
      parentScope.bindings.set(node.name.text, node);
    const scoped =
      ts.isSourceFile(node) ||
      ts.isBlock(node) ||
      ts.isFunctionLike(node) ||
      ts.isForStatement(node) ||
      ts.isForInStatement(node) ||
      ts.isForOfStatement(node) ||
      ts.isCatchClause(node);
    const scope = scoped
      ? { parent: parentScope, bindings: new Map() }
      : parentScope;
    scopes.set(node, scope);
    if (
      (ts.isVariableDeclaration(node) || ts.isParameter(node)) &&
      ts.isIdentifier(node.name)
    ) {
      scope.bindings.set(node.name.text, node);
    }
    if (ts.isCallExpression(node)) {
      calls.push(node);
      if (
        ts.isPropertyAccessExpression(node.expression) &&
        ["set", "append", "delete"].includes(node.expression.name.text)
      )
        writes.push(node);
    }
    if (
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind === ts.SyntaxKind.EqualsToken
    ) {
      writes.push(node);
    }
    for (const position of [node.pos, node.end]) {
      for (const range of [
        ...(ts.getLeadingCommentRanges(source, position) ?? []),
        ...(ts.getTrailingCommentRanges(source, position) ?? []),
      ])
        comments.set(range.pos, range);
    }
    ts.forEachChild(node, (child) => index(child, scope));
  }
  index(file, { bindings: new Map() });
  writes.sort((a, b) => a.pos - b.pos);
  const disabled = new Set();
  for (const range of comments.values()) {
    const match =
      /custom-lint:\s*disable(-next)?=([a-z0-9-]+(?:\s*,\s*[a-z0-9-]+)*)/.exec(
        source.slice(range.pos, range.end),
      );
    if (
      match?.[2]
        .split(",")
        .map((rule) => rule.trim())
        .includes(RULE)
    ) {
      disabled.add(
        file.getLineAndCharacterOfPosition(range.pos).line +
          1 +
          (match[1] ? 1 : 0),
      );
    }
  }
  function binding(node) {
    if (!node || !ts.isIdentifier(node)) return undefined;
    for (let scope = scopes.get(node); scope; scope = scope.parent) {
      if (scope.bindings.has(node.text)) return scope.bindings.get(node.text);
    }
  }
  function originalBinding(declaration, seen = new Set()) {
    if (!declaration || seen.has(declaration)) return declaration;
    const original = binding(declaration.initializer);
    return original
      ? originalBinding(original, new Set(seen).add(declaration))
      : declaration;
  }
  function definite(write, use) {
    const ancestors = new Set();
    for (let node = use; node; node = node.parent) ancestors.add(node);
    for (
      let child = write, node = write.parent;
      node && !ts.isFunctionLike(node);
      child = node, node = node.parent
    ) {
      if (
        (ts.isIfStatement(node) || ts.isConditionalExpression(node)) &&
        !ancestors.has(child)
      )
        return false;
      if (
        (ts.isIfStatement(node) ||
          ts.isConditionalExpression(node) ||
          ts.isIterationStatement(node, false) ||
          ts.isCaseClause(node) ||
          ts.isCatchClause(node) ||
          (ts.isBinaryExpression(node) &&
            [
              ts.SyntaxKind.AmpersandAmpersandToken,
              ts.SyntaxKind.BarBarToken,
              ts.SyntaxKind.QuestionQuestionToken,
            ].includes(node.operatorToken.kind))) &&
        !ancestors.has(node)
      )
        return false;
    }
    return true;
  }
  // Resolve local values at their use site; later or conditional writes cannot
  // establish visibility. Unknown query text never proves the parameter exists.
  function evaluate(
    node,
    use = node,
    argumentsByBinding = new Map(),
    seen = new Set(),
  ) {
    if (!node || seen.has(node) || seen.size > 40) return text();
    const next = new Set(seen).add(node);
    const ev = (child, at = child) =>
      evaluate(child, at, argumentsByBinding, next);
    if (ts.isStringLiteralLike(node) || ts.isNumericLiteral(node))
      return text(node.text);
    if (
      ts.isParenthesizedExpression(node) ||
      ts.isAsExpression(node) ||
      ts.isNonNullExpression(node) ||
      ts.isAwaitExpression(node) ||
      ts.isSatisfiesExpression(node)
    )
      return ev(node.expression, use);
    if (ts.isIdentifier(node)) {
      const declaration = binding(node);
      if (argumentsByBinding.has(declaration))
        return argumentsByBinding.get(declaration);
      let value = ev(declaration?.initializer);
      if (!declaration) return value;
      for (const write of writes) {
        if (
          write.pos < declaration.end ||
          write.pos >= use.pos ||
          functionOf(write) !== functionOf(use)
        )
          continue;
        const target = ts.isCallExpression(write)
          ? write.expression.expression
          : write.left;
        const targetDeclaration = binding(target);
        if (targetDeclaration !== declaration) {
          if (
            ts.isCallExpression(write) &&
            write.expression.name.text === "delete" &&
            value.params &&
            originalBinding(targetDeclaration) ===
              originalBinding(declaration) &&
            strings(ev(write.arguments[0])).some(
              (key) => key === UNKNOWN || key === "visibility",
            )
          )
            value = { ...text(), params: true };
          continue;
        }
        const guaranteed = definite(write, use);
        if (ts.isBinaryExpression(write)) {
          const assigned = ev(write.right);
          value = guaranteed ? assigned : choices([value, assigned]);
          continue;
        }
        if (!value.params) continue;
        const keys = strings(ev(write.arguments[0]));
        const operation = write.expression.name.text;
        if (keys.includes(UNKNOWN)) {
          if (operation === "delete") value = { ...text(), params: true };
          continue;
        }
        if (!guaranteed && operation !== "delete") continue;
        value = {
          params: true,
          strings: updateQueries(
            strings(value),
            keys,
            operation === "delete" ? [""] : strings(ev(write.arguments[1])),
            operation,
          ),
        };
      }
      return value;
    }
    if (ts.isObjectLiteralExpression(node)) {
      const fields = new Map();
      for (const property of node.properties) {
        if (ts.isSpreadAssignment(property)) {
          for (const [key, value] of ev(property.expression).fields ?? [])
            fields.set(key, value);
        } else {
          const key = nameOf(property.name);
          if (key)
            fields.set(
              key,
              ev(
                ts.isShorthandPropertyAssignment(property)
                  ? property.name
                  : property.initializer,
              ),
            );
        }
      }
      return { fields };
    }
    if (ts.isTemplateExpression(node)) {
      let result = [node.head.text];
      for (const span of node.templateSpans) {
        result = result
          .flatMap((prefix) =>
            strings(ev(span.expression)).map(
              (part) => prefix + part + span.literal.text,
            ),
          )
          .slice(0, 32);
      }
      return { strings: result };
    }
    if (
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind === ts.SyntaxKind.PlusToken
    ) {
      return {
        strings: strings(ev(node.left))
          .flatMap((left) =>
            strings(ev(node.right)).map((right) => left + right),
          )
          .slice(0, 32),
      };
    }
    if (ts.isConditionalExpression(node))
      return choices([ev(node.whenTrue), ev(node.whenFalse)]);
    if (ts.isNewExpression(node)) {
      const constructor = nameOf(node.expression);
      if (constructor === "URLSearchParams") {
        const value = node.arguments?.length ? ev(node.arguments[0]) : text("");
        let queries = strings(value);
        if (value.fields) {
          queries = [""];
          for (const [key, val] of value.fields)
            queries = updateQueries(queries, [key], strings(val));
        }
        return { params: true, strings: queries };
      }
      if (constructor === "URL") return ev(node.arguments?.[0]);
      if (constructor === "Request")
        return {
          ...ev(node.arguments?.[0]),
          requestOptions: ev(node.arguments?.[1]),
        };
    }
    if (ts.isPropertyAccessExpression(node)) {
      const value = ev(node.expression, use);
      if (["href", "search"].includes(node.name.text)) return value;
      return value.fields?.get(node.name.text) ?? text();
    }
    if (ts.isCallExpression(node)) {
      if (
        ts.isPropertyAccessExpression(node.expression) &&
        node.expression.name.text === "toString"
      ) {
        return ev(node.expression.expression, node);
      }
      const declaration = binding(node.expression);
      const fn =
        declaration &&
        (ts.isFunctionDeclaration(declaration)
          ? declaration
          : declaration.initializer);
      if (fn && ts.isFunctionLike(fn) && fn.body) {
        const args = new Map(argumentsByBinding);
        fn.parameters.forEach((parameter, index) =>
          args.set(
            parameter,
            node.arguments[index]
              ? ev(node.arguments[index])
              : ev(parameter.initializer),
          ),
        );
        const returns = [];
        function collect(child) {
          if (ts.isReturnStatement(child))
            returns.push(evaluate(child.expression, child, args, next));
          else if (!ts.isFunctionLike(child)) ts.forEachChild(child, collect);
        }
        if (ts.isBlock(fn.body)) {
          collect(fn.body);
          if (!ts.isReturnStatement(fn.body.statements.at(-1) ?? fn.body))
            returns.push(text());
        } else returns.push(evaluate(fn.body, fn.body, args, next));
        return choices(returns);
      }
    }
    return text();
  }
  const findings = [];
  for (const call of calls) {
    const callee = call.expression;
    const name = ts.isPropertyAccessExpression(callee)
      ? callee.name.text
      : nameOf(callee);
    if (
      !["fetch", "authenticatedFetch", "hostFetch", "get", "request"].includes(
        name,
      )
    )
      continue;
    let url = evaluate(call.arguments[0]);
    let options = evaluate(call.arguments[1]);
    if (url.requestOptions)
      options = {
        fields: new Map([
          ...(url.requestOptions.fields ?? []),
          ...(options.fields ?? []),
        ]),
      };
    if (name === "request" && url.fields) {
      options = url;
      url = options.fields.get("url") ?? options.fields.get("uri") ?? text();
    }
    const method = options.fields?.get("method");
    if (
      name !== "get" &&
      method &&
      strings(method).every(
        (value) => value !== UNKNOWN && value.toUpperCase() !== "GET",
      )
    )
      continue;
    const query = ["get", "request"].includes(name)
      ? (options.fields?.get("params") ?? options.fields?.get("searchParams"))
      : undefined;
    const explicitQuery =
      (query?.fields?.has("visibility") &&
        strings(query.fields.get("visibility")).every(
          (value) => value !== "",
        )) ||
      (query?.params && strings(query).every(hasVisibility));
    const missing = strings(url).some((value) => {
      const pathname = value.split(/[?#]/, 1)[0];
      const index = pathname.lastIndexOf("/v1/sessions");
      if (index === -1) return false;
      const suffix = pathname.slice(index + "/v1/sessions".length);
      if (suffix !== "" && suffix !== "/" && !suffix.startsWith(UNKNOWN))
        return false;
      return (
        !explicitQuery &&
        !hasVisibility(
          value.slice(index).split("#")[0].split("?").slice(1).join("?"),
        )
      );
    });
    if (!missing) continue;
    const line =
      file.getLineAndCharacterOfPosition(call.getStart(file)).line + 1;
    if (!disabled.has(line))
      findings.push({
        path: filename,
        line,
        message:
          "Session-list requests must specify visibility explicitly (for example, visibility=all).",
      });
  }
  return findings;
}

function hasVisibility(query) {
  const visibility = new URLSearchParams(query).get("visibility");
  return visibility !== null && visibility !== "";
}

if (
  process.argv[1] &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  try {
    const findings = process.argv
      .slice(2)
      .flatMap((filename) =>
        analyzeSource(filename, readFileSync(filename, "utf8")),
      );
    process.stdout.write(`${JSON.stringify(findings)}\n`);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}
