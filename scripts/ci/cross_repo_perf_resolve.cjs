"use strict";

const COMMAND_RE =
  /^@cross-repo-perf: https:\/\/github\.com\/tile-ai\/tilelang-metax\/pull\/([1-9][0-9]*)\/?$/;
const ALLOWED_PERMISSIONS = new Set(["write", "maintain", "admin"]);
const TILELANG_REPOSITORY = "tile-ai/tilelang-metax";
const MAX_COMMENT_LENGTH = 60000;

function parseCommand(body) {
  if (typeof body !== "string") {
    return null;
  }
  const match = COMMAND_RE.exec(body.trim());
  if (!match) {
    return null;
  }
  const tilelangPrNumber = Number(match[1]);
  if (!Number.isSafeInteger(tilelangPrNumber) || tilelangPrNumber <= 0) {
    return null;
  }
  return { tilelangPrNumber };
}

function permissionAllowed(permission) {
  return typeof permission === "string" && ALLOWED_PERMISSIONS.has(permission);
}

function splitRepository(repository) {
  const parts = repository.split("/");
  if (parts.length !== 2 || parts.some((part) => part.length === 0)) {
    throw new Error(`invalid repository identity: ${repository}`);
  }
  return { owner: parts[0], repo: parts[1] };
}

async function repositoryPermission(github, repository, username) {
  const { owner, repo } = splitRepository(repository);
  const response = await github.rest.repos.getCollaboratorPermissionLevel({
    owner,
    repo,
    username,
  });
  return response.data.permission || response.data.role_name || "";
}

async function defaultBranchIdentity(github, repository) {
  const { owner, repo } = splitRepository(repository);
  const repositoryResponse = await github.rest.repos.get({ owner, repo });
  const defaultBranch = repositoryResponse.data.default_branch;
  if (typeof defaultBranch !== "string" || defaultBranch.length === 0) {
    throw new Error(`${repository} has no API-reported default branch`);
  }
  const branchResponse = await github.rest.repos.getBranch({
    owner,
    repo,
    branch: defaultBranch,
  });
  const defaultSha = branchResponse.data.commit && branchResponse.data.commit.sha;
  if (typeof defaultSha !== "string" || defaultSha.length === 0) {
    throw new Error(`${repository} default branch has no commit SHA`);
  }
  return { defaultBranch, defaultSha };
}

async function pullWithMergeability(
  github,
  repository,
  pullNumber,
  mergeableAttempts,
  sleep,
) {
  const { owner, repo } = splitRepository(repository);
  let pull;
  for (let attempt = 0; attempt < mergeableAttempts; attempt += 1) {
    const response = await github.rest.pulls.get({ owner, repo, pull_number: pullNumber });
    pull = response.data;
    if (pull.mergeable === true || pull.mergeable === false) {
      return { pull, mergeabilityAvailable: true };
    }
    if (pull.mergeable !== null) {
      return { pull, mergeabilityAvailable: false, mergeabilityMalformed: true };
    }
    if (attempt + 1 < mergeableAttempts) {
      await sleep(attempt + 1);
    }
  }
  return { pull, mergeabilityAvailable: false, mergeabilityMalformed: false };
}

function reject(reason) {
  return { disposition: "reject", reason };
}

function isNonemptyString(value) {
  return typeof value === "string" && value.length > 0;
}

function isCommitSha(value) {
  return typeof value === "string" && /^[0-9a-f]{40}$/.test(value);
}

function validatePull(
  repository,
  pull,
  defaults,
  mergeabilityAvailable,
  mergeabilityMalformed = false,
) {
  if (pull.state !== "open") {
    return `${repository} pull request must be open`;
  }
  if (!Number.isSafeInteger(pull.number) || pull.number <= 0) {
    return `${repository} pull request number is invalid`;
  }
  if (!isNonemptyString(pull.html_url)) {
    return `${repository} pull request URL is missing`;
  }
  if (!pull.user || !isNonemptyString(pull.user.login)) {
    return `${repository} pull request author is missing`;
  }
  if (!pull.head || !pull.head.repo || pull.head.repo.full_name !== repository) {
    return `${repository} pull request head must be a branch in the same repository`;
  }
  if (!isNonemptyString(pull.head.ref)) {
    return `${repository} pull request head ref is missing`;
  }
  if (!isCommitSha(pull.head.sha)) {
    return `${repository} pull request head SHA is invalid`;
  }
  if (!pull.base || pull.base.ref !== defaults.defaultBranch) {
    return `${repository} pull request must target the default branch`;
  }
  if (!isCommitSha(pull.base.sha) || pull.base.sha !== defaults.defaultSha) {
    return `${repository} pull request base SHA is stale relative to the default branch`;
  }
  if (mergeabilityMalformed) {
    return `${repository} pull request mergeability value is malformed`;
  }
  if (!mergeabilityAvailable) {
    return `${repository} pull request mergeability is unavailable after bounded retries`;
  }
  if (pull.mergeable === false) {
    return `${repository} pull request has a merge conflict`;
  }
  if (!isCommitSha(pull.merge_commit_sha)) {
    return `${repository} pull request has no merge SHA`;
  }
  return null;
}

function pullIdentity(repository, pull, defaults) {
  return {
    repository,
    pr_number: pull.number,
    pr_url: pull.html_url,
    author: pull.user.login,
    default_branch: defaults.defaultBranch,
    default_sha: defaults.defaultSha,
    base_ref: pull.base.ref,
    base_sha: pull.base.sha,
    head_ref: pull.head.ref,
    head_sha: pull.head.sha,
    merge_sha: pull.merge_commit_sha,
  };
}

async function resolveRequest({
  github,
  context,
  body,
  harnessSha256 = "",
  mergeableAttempts = 5,
  sleep = (attempt) => new Promise((resolve) => setTimeout(resolve, attempt * 1000)),
}) {
  const parsed = parseCommand(body);
  if (!parsed) {
    return { disposition: "ignore", reason: "comment does not match the command" };
  }
  if (!context.payload || !context.payload.issue || !context.payload.issue.pull_request) {
    return { disposition: "ignore", reason: "issue is not a pull request" };
  }
  if (typeof harnessSha256 !== "string" || !/^[0-9a-f]{64}$/.test(harnessSha256)) {
    throw new Error("trusted harness SHA-256 is required");
  }
  if (!Number.isInteger(mergeableAttempts) || mergeableAttempts < 1 || mergeableAttempts > 10) {
    throw new Error("mergeableAttempts must be an integer between 1 and 10");
  }

  const tileopsRepository = `${context.repo.owner}/${context.repo.repo}`;
  const tileopsPullNumber = context.issue.number;
  const actor = context.actor || context.payload.comment.user.login;

  const actorPermission = await repositoryPermission(github, tileopsRepository, actor);
  if (!permissionAllowed(actorPermission)) {
    return reject("trigger actor lacks write-equivalent TileOps permission");
  }

  const [tileopsDefaults, tilelangDefaults] = await Promise.all([
    defaultBranchIdentity(github, tileopsRepository),
    defaultBranchIdentity(github, TILELANG_REPOSITORY),
  ]);
  const tileopsResult = await pullWithMergeability(
    github,
    tileopsRepository,
    tileopsPullNumber,
    mergeableAttempts,
    sleep,
  );
  const tilelangResult = await pullWithMergeability(
    github,
    TILELANG_REPOSITORY,
    parsed.tilelangPrNumber,
    mergeableAttempts,
    sleep,
  );

  const tileopsError = validatePull(
    tileopsRepository,
    tileopsResult.pull,
    tileopsDefaults,
    tileopsResult.mergeabilityAvailable,
    tileopsResult.mergeabilityMalformed,
  );
  if (tileopsError) {
    return reject(tileopsError);
  }
  const tilelangError = validatePull(
    TILELANG_REPOSITORY,
    tilelangResult.pull,
    tilelangDefaults,
    tilelangResult.mergeabilityAvailable,
    tilelangResult.mergeabilityMalformed,
  );
  if (tilelangError) {
    return reject(tilelangError);
  }

  const tileopsAuthorPermission = await repositoryPermission(
    github,
    tileopsRepository,
    tileopsResult.pull.user.login,
  );
  if (!permissionAllowed(tileopsAuthorPermission)) {
    return reject("TileOps PR author lacks write-equivalent repository permission");
  }
  const tilelangAuthorPermission = await repositoryPermission(
    github,
    TILELANG_REPOSITORY,
    tilelangResult.pull.user.login,
  );
  if (!permissionAllowed(tilelangAuthorPermission)) {
    return reject("TileLang PR author lacks write-equivalent repository permission");
  }

  return {
    schema_version: 1,
    disposition: "run",
    reason: "",
    run_id: context.runId,
    run_attempt: context.runAttempt,
    trigger_comment_id: context.payload.comment.id,
    trigger_actor: actor,
    harness_sha256: harnessSha256,
    tileops: pullIdentity(tileopsRepository, tileopsResult.pull, tileopsDefaults),
    tilelang: pullIdentity(TILELANG_REPOSITORY, tilelangResult.pull, tilelangDefaults),
  };
}

function boundedComment(marker, body) {
  if (typeof marker !== "string" || marker.length === 0) {
    throw new Error("comment marker is required");
  }
  const prefix = `${marker}\n`;
  if (prefix.length > MAX_COMMENT_LENGTH) {
    throw new Error("comment marker exceeds the maximum comment size");
  }
  const content = typeof body === "string" ? body : String(body ?? "");
  return `${prefix}${content.slice(0, MAX_COMMENT_LENGTH - prefix.length)}`;
}

async function upsertBotComment({ github, owner, repo, issueNumber, marker, body }) {
  const rendered = boundedComment(marker, body);
  const comments = await github.paginate(github.rest.issues.listComments, {
    owner,
    repo,
    issue_number: issueNumber,
    per_page: 100,
  });
  const existing = comments.find(
    (comment) =>
      comment.user &&
      comment.user.login === "github-actions[bot]" &&
      typeof comment.body === "string" &&
      comment.body.includes(marker),
  );
  if (existing) {
    const response = await github.rest.issues.updateComment({
      owner,
      repo,
      comment_id: existing.id,
      body: rendered,
    });
    return { action: "updated", comment: response.data };
  }
  const response = await github.rest.issues.createComment({
    owner,
    repo,
    issue_number: issueNumber,
    body: rendered,
  });
  return { action: "created", comment: response.data };
}

module.exports = {
  parseCommand,
  permissionAllowed,
  resolveRequest,
  upsertBotComment,
};
