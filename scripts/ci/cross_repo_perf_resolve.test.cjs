const assert = require("node:assert/strict");
const test = require("node:test");

const {
  parseCommand,
  permissionAllowed,
  resolveRequest,
  upsertBotComment,
} = require("./cross_repo_perf_resolve.cjs");

const TILEOPS_REPO = "MetaX-MACA/TileOPs-Metax";
const TILELANG_REPO = "tile-ai/tilelang-metax";
const SHA = {
  tileopsDefault: "1".repeat(40),
  tileopsHead: "2".repeat(40),
  tileopsMerge: "3".repeat(40),
  tilelangDefault: "4".repeat(40),
  tilelangHead: "5".repeat(40),
  tilelangMerge: "6".repeat(40),
};

function command(number = 90, slash = "") {
  return `@cross-repo-perf: https://github.com/${TILELANG_REPO}/pull/${number}${slash}`;
}

function makePr({
  repository,
  number,
  author,
  defaultSha,
  headSha,
  mergeSha,
  state = "open",
  baseRef = "dev",
  baseSha = defaultSha,
  headRepository = repository,
  headRef = `feature-${number}`,
  mergeable = true,
  htmlUrl = `https://github.com/${repository}/pull/${number}`,
}) {
  return {
    number,
    state,
    html_url: htmlUrl,
    user: { login: author },
    base: { ref: baseRef, sha: baseSha, repo: { full_name: repository } },
    head: { ref: headRef, sha: headSha, repo: { full_name: headRepository } },
    mergeable,
    merge_commit_sha: mergeSha,
  };
}

function makeFixture(overrides = {}) {
  const tileops = makePr({
    repository: TILEOPS_REPO,
    number: 42,
    author: "tileops-author",
    defaultSha: SHA.tileopsDefault,
    headSha: SHA.tileopsHead,
    mergeSha: SHA.tileopsMerge,
    ...(overrides.tileops || {}),
  });
  const tilelang = makePr({
    repository: TILELANG_REPO,
    number: 90,
    author: "tilelang-author",
    defaultSha: SHA.tilelangDefault,
    headSha: SHA.tilelangHead,
    mergeSha: SHA.tilelangMerge,
    ...(overrides.tilelang || {}),
  });
  const permissions = {
    [`${TILEOPS_REPO}:maintainer`]: "write",
    [`${TILEOPS_REPO}:tileops-author`]: "maintain",
    [`${TILELANG_REPO}:tilelang-author`]: "admin",
    ...(overrides.permissions || {}),
  };
  const pullSequences = new Map([
    [`${TILEOPS_REPO}#42`, [tileops]],
    [`${TILELANG_REPO}#90`, [tilelang]],
  ]);
  for (const [key, value] of Object.entries(overrides.pullSequences || {})) {
    pullSequences.set(key, value);
  }
  const calls = [];
  const github = {
    rest: {
      pulls: {
        async get({ owner, repo, pull_number }) {
          const key = `${owner}/${repo}#${pull_number}`;
          calls.push(["pulls.get", key]);
          const sequence = pullSequences.get(key);
          if (!sequence || sequence.length === 0) {
            throw new Error(`unexpected pull lookup ${key}`);
          }
          const index = Math.min(
            calls.filter(([name, value]) => name === "pulls.get" && value === key).length - 1,
            sequence.length - 1,
          );
          return { data: sequence[index] };
        },
      },
      repos: {
        async get({ owner, repo }) {
          const repository = `${owner}/${repo}`;
          calls.push(["repos.get", repository]);
          return { data: { default_branch: "dev", full_name: repository } };
        },
        async getBranch({ owner, repo, branch }) {
          const repository = `${owner}/${repo}`;
          calls.push(["repos.getBranch", `${repository}:${branch}`]);
          const sha = repository === TILEOPS_REPO ? SHA.tileopsDefault : SHA.tilelangDefault;
          return { data: { commit: { sha } } };
        },
        async getCollaboratorPermissionLevel({ owner, repo, username }) {
          const key = `${owner}/${repo}:${username}`;
          calls.push(["repos.getCollaboratorPermissionLevel", key]);
          return { data: { permission: permissions[key] || "read" } };
        },
      },
    },
  };
  const context = {
    actor: "maintainer",
    runId: 123456,
    runAttempt: 2,
    repo: { owner: "MetaX-MACA", repo: "TileOPs-Metax" },
    issue: { number: 42 },
    payload: {
      comment: { id: 9988, body: command(), user: { login: "maintainer" } },
      issue: { number: 42, pull_request: { url: "https://api.github.test/pulls/42" } },
    },
  };
  return { github, context, calls };
}

test("parseCommand accepts only the exact TileLang PR command", () => {
  assert.deepEqual(parseCommand(command()), { tilelangPrNumber: 90 });
  assert.deepEqual(parseCommand(command(90, "/")), { tilelangPrNumber: 90 });
  assert.deepEqual(parseCommand(` \r\n${command()}\r\n `), { tilelangPrNumber: 90 });
});

test("parseCommand rejects malformed or ambiguous bodies", () => {
  const rejected = [
    `please run ${command()}`,
    `${command()} now`,
    `${command()}\n${command()}`,
    `${command()}?foo=bar`,
    `${command()}#fragment`,
    "@cross-repo-perf: https://github.com/tile-ai/tilelang/pull/90",
    "@cross-repo-perf: https://github.com/other/tilelang-metax/pull/90",
    "@cross-repo-perf: https://github.com/tile-ai/tilelang-metax/pull/0",
    "@cross-repo-perf: https://github.com/tile-ai/tilelang-metax/pull/-1",
    "@cross-repo-perf: https://github.com/tile-ai/tilelang-metax/pull/not-a-number",
    "@cross-repo-perf:https://github.com/tile-ai/tilelang-metax/pull/90",
    "",
    "   \r\n",
  ];
  for (const body of rejected) {
    assert.equal(parseCommand(body), null, body);
  }
});

test("permissionAllowed accepts only write-equivalent repository roles", () => {
  for (const permission of ["write", "maintain", "admin"]) {
    assert.equal(permissionAllowed(permission), true);
  }
  for (const permission of ["read", "triage", "none", "WRITE", "", null, undefined]) {
    assert.equal(permissionAllowed(permission), false);
  }
});

test("resolveRequest ignores non-command comments and non-PR issues", async () => {
  const fixture = makeFixture();
  assert.deepEqual(
    await resolveRequest({
      github: fixture.github,
      context: fixture.context,
      body: "ordinary review comment",
    }),
    { disposition: "ignore", reason: "comment does not match the command" },
  );
  assert.equal(fixture.calls.length, 0);

  const issueContext = structuredClone(fixture.context);
  delete issueContext.payload.issue.pull_request;
  assert.deepEqual(
    await resolveRequest({ github: fixture.github, context: issueContext, body: command() }),
    { disposition: "ignore", reason: "issue is not a pull request" },
  );
});

test("resolveRequest returns immutable baseline and candidate identities", async () => {
  const fixture = makeFixture();
  const result = await resolveRequest({
    github: fixture.github,
    context: fixture.context,
    body: command(),
    harnessSha256: "a".repeat(64),
    sleep: async () => {},
  });

  assert.equal(result.disposition, "run");
  assert.equal(result.schema_version, 1);
  assert.equal(result.run_id, 123456);
  assert.equal(result.run_attempt, 2);
  assert.equal(result.trigger_comment_id, 9988);
  assert.equal(result.trigger_actor, "maintainer");
  assert.equal(result.harness_sha256, "a".repeat(64));
  assert.deepEqual(result.tileops, {
    repository: TILEOPS_REPO,
    pr_number: 42,
    pr_url: `https://github.com/${TILEOPS_REPO}/pull/42`,
    author: "tileops-author",
    default_branch: "dev",
    default_sha: SHA.tileopsDefault,
    base_ref: "dev",
    base_sha: SHA.tileopsDefault,
    head_ref: "feature-42",
    head_sha: SHA.tileopsHead,
    merge_sha: SHA.tileopsMerge,
  });
  assert.deepEqual(result.tilelang, {
    repository: TILELANG_REPO,
    pr_number: 90,
    pr_url: `https://github.com/${TILELANG_REPO}/pull/90`,
    author: "tilelang-author",
    default_branch: "dev",
    default_sha: SHA.tilelangDefault,
    base_ref: "dev",
    base_sha: SHA.tilelangDefault,
    head_ref: "feature-90",
    head_sha: SHA.tilelangHead,
    merge_sha: SHA.tilelangMerge,
  });
});

test("resolveRequest requires a trusted harness SHA-256", async () => {
  for (const harnessSha256 of [undefined, "", "abc", "g".repeat(64), "a".repeat(63)]) {
    const fixture = makeFixture();
    await assert.rejects(
      resolveRequest({
        github: fixture.github,
        context: fixture.context,
        body: command(),
        harnessSha256,
        sleep: async () => {},
      }),
      /harness.*sha-256/i,
    );
  }
});

test("resolveRequest propagates unexpected Octokit failures", async () => {
  const fixture = makeFixture();
  fixture.github.rest.repos.get = async () => {
    throw new Error("octokit transport failed");
  };
  await assert.rejects(
    resolveRequest({
      github: fixture.github,
      context: fixture.context,
      body: command(),
      harnessSha256: "f".repeat(64),
      sleep: async () => {},
    }),
    /octokit transport failed/,
  );
});

test("resolveRequest retries a null mergeability result with a fixed bound", async () => {
  const first = makePr({
    repository: TILELANG_REPO,
    number: 90,
    author: "tilelang-author",
    defaultSha: SHA.tilelangDefault,
    headSha: SHA.tilelangHead,
    mergeSha: SHA.tilelangMerge,
    mergeable: null,
  });
  const second = { ...first, mergeable: true };
  const fixture = makeFixture({ pullSequences: { [`${TILELANG_REPO}#90`]: [first, second] } });
  let sleeps = 0;
  const result = await resolveRequest({
    github: fixture.github,
    context: fixture.context,
    body: command(),
    harnessSha256: "b".repeat(64),
    mergeableAttempts: 3,
    sleep: async () => {
      sleeps += 1;
    },
  });
  assert.equal(result.disposition, "run");
  assert.equal(sleeps, 1);
  assert.equal(
    fixture.calls.filter(([name, key]) => name === "pulls.get" && key === `${TILELANG_REPO}#90`).length,
    2,
  );
});

test("resolveRequest rejects after the mergeability retry bound", async () => {
  const pending = makePr({
    repository: TILELANG_REPO,
    number: 90,
    author: "tilelang-author",
    defaultSha: SHA.tilelangDefault,
    headSha: SHA.tilelangHead,
    mergeSha: SHA.tilelangMerge,
    mergeable: null,
  });
  const fixture = makeFixture({
    pullSequences: { [`${TILELANG_REPO}#90`]: [pending, pending, pending] },
  });
  const result = await resolveRequest({
    github: fixture.github,
    context: fixture.context,
    body: command(),
    harnessSha256: "c".repeat(64),
    mergeableAttempts: 3,
    sleep: async () => {},
  });
  assert.equal(result.disposition, "reject");
  assert.match(result.reason, /mergeability.*unavailable/i);
});

test("resolveRequest rejects untrusted permissions", async (t) => {
  const cases = [
    ["trigger actor", { [`${TILEOPS_REPO}:maintainer`]: "read" }],
    ["TileOps PR author", { [`${TILEOPS_REPO}:tileops-author`]: "triage" }],
    ["TileLang PR author", { [`${TILELANG_REPO}:tilelang-author`]: "read" }],
  ];
  for (const [label, permissions] of cases) {
    await t.test(label, async () => {
      const fixture = makeFixture({ permissions });
      const result = await resolveRequest({
        github: fixture.github,
        context: fixture.context,
        body: command(),
        harnessSha256: "d".repeat(64),
        sleep: async () => {},
      });
      assert.equal(result.disposition, "reject");
      assert.match(result.reason, /permission/i);
    });
  }
});

test("resolveRequest treats a collaborator permission 404 as no permission", async () => {
  const fixture = makeFixture();
  fixture.github.rest.repos.getCollaboratorPermissionLevel = async () => {
    const error = new Error("Not Found");
    error.status = 404;
    throw error;
  };
  const result = await resolveRequest({
    github: fixture.github,
    context: fixture.context,
    body: command(),
    harnessSha256: "f".repeat(64),
    sleep: async () => {},
  });
  assert.equal(result.disposition, "reject");
  assert.match(result.reason, /permission/i);
  assert.equal(fixture.calls.some(([name]) => name === "pulls.get"), false);
});

test("resolveRequest rejects invalid PR trust and ref states", async (t) => {
  const cases = [
    ["closed TileOps PR", { tileops: { state: "closed" } }, /open/i],
    ["closed TileLang PR", { tilelang: { state: "closed" } }, /open/i],
    ["TileOps fork head", { tileops: { headRepository: "fork/TileOPs-Metax" } }, /same repository/i],
    ["TileLang fork head", { tilelang: { headRepository: "fork/tilelang-metax" } }, /same repository/i],
    ["wrong default target", { tilelang: { baseRef: "main" } }, /default branch/i],
    ["stale base SHA", { tileops: { baseSha: "9".repeat(40) } }, /base sha/i],
    ["conflict", { tilelang: { mergeable: false } }, /conflict/i],
    ["malformed mergeability", { tilelang: { mergeable: "yes" } }, /mergeability/i],
    ["missing merge SHA", { tileops: { mergeSha: null } }, /merge sha/i],
    ["missing PR URL", { tileops: { htmlUrl: null } }, /url/i],
    ["missing head ref", { tilelang: { headRef: "" } }, /head ref/i],
    ["missing head SHA", { tileops: { headSha: null } }, /head sha/i],
  ];
  for (const [label, overrides, reason] of cases) {
    await t.test(label, async () => {
      const fixture = makeFixture(overrides);
      const result = await resolveRequest({
        github: fixture.github,
        context: fixture.context,
        body: command(),
        harnessSha256: "e".repeat(64),
        sleep: async () => {},
      });
      assert.equal(result.disposition, "reject");
      assert.match(result.reason, reason);
    });
  }
});

function makeCommentGithub(comments) {
  const calls = [];
  const github = {
    paginate: async (method, params) => {
      calls.push(["paginate", method, params]);
      return comments;
    },
    rest: {
      issues: {
        listComments: Symbol("listComments"),
        async updateComment(params) {
          calls.push(["updateComment", params]);
          return { data: { id: params.comment_id, body: params.body } };
        },
        async createComment(params) {
          calls.push(["createComment", params]);
          return { data: { id: 777, body: params.body } };
        },
      },
    },
  };
  return { github, calls };
}

test("upsertBotComment updates only a marker-matched bot comment", async () => {
  const marker = "<!-- cross-repo-perf:9988 -->";
  const fixture = makeCommentGithub([
    { id: 10, user: { login: "reviewer" }, body: `${marker}\nforged` },
    { id: 11, user: { login: "github-actions[bot]" }, body: "other report" },
    { id: 12, user: { login: "github-actions[bot]" }, body: `${marker}\nold` },
  ]);
  const result = await upsertBotComment({
    github: fixture.github,
    owner: "MetaX-MACA",
    repo: "TileOPs-Metax",
    issueNumber: 42,
    marker,
    body: "new report",
  });
  assert.equal(result.action, "updated");
  const update = fixture.calls.find(([name]) => name === "updateComment");
  assert.equal(update[1].comment_id, 12);
  assert.equal(update[1].body, `${marker}\nnew report`);
  assert.equal(fixture.calls.filter(([name]) => name === "createComment").length, 0);
});

test("upsertBotComment creates a comment when no bot marker exists", async () => {
  const marker = "<!-- cross-repo-perf:9988 -->";
  const fixture = makeCommentGithub([
    { id: 10, user: { login: "reviewer" }, body: `${marker}\nforged` },
    {
      id: 11,
      user: { login: "github-actions[bot]" },
      body: `unrelated report quoting ${marker} in the middle`,
    },
  ]);
  const result = await upsertBotComment({
    github: fixture.github,
    owner: "MetaX-MACA",
    repo: "TileOPs-Metax",
    issueNumber: 42,
    marker,
    body: "report",
  });
  assert.equal(result.action, "created");
  const create = fixture.calls.find(([name]) => name === "createComment");
  assert.equal(create[1].body, `${marker}\nreport`);
});

test("upsertBotComment bounds the complete comment to 60000 characters", async () => {
  const marker = "<!-- cross-repo-perf:9988 -->";
  const fixture = makeCommentGithub([]);
  await upsertBotComment({
    github: fixture.github,
    owner: "MetaX-MACA",
    repo: "TileOPs-Metax",
    issueNumber: 42,
    marker,
    body: "x".repeat(70000),
  });
  const create = fixture.calls.find(([name]) => name === "createComment");
  assert.equal(create[1].body.length, 60000);
  assert.ok(create[1].body.startsWith(`${marker}\n`));
});
