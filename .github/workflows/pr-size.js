// Computes a `size/*` label for a PR from its added + deleted lines,
// excluding generated / lock files, and reconciles the label on the PR.

const GENERATED = [/^uv\.lock$/, /package-lock\.json$/, /yarn\.lock$/];

const THRESHOLDS = {
  XS: 9,
  S: 49,
  M: 199,
  L: 499,
  XL: Infinity,
};

function isGenerated(filename) {
  return GENERATED.some((p) => p.test(filename));
}

function getSize(total) {
  return Object.entries(THRESHOLDS).find(([, max]) => total <= max)[0];
}

module.exports = async ({ github, context }) => {
  const { owner, repo } = context.repo;
  const pr = context.payload.pull_request;

  const files = await github.paginate(github.rest.pulls.listFiles, {
    owner,
    repo,
    pull_number: pr.number,
    per_page: 100,
  });

  const maxThreshold = Math.max(...Object.values(THRESHOLDS).filter(isFinite));
  let total = 0;
  for (const f of files) {
    if (!isGenerated(f.filename)) {
      total += f.additions + f.deletions;
    }
    if (total > maxThreshold) break;
  }

  const sizeLabel = `size/${getSize(total)}`;
  console.log(`Size: ${total} lines -> ${sizeLabel}`);

  const currentLabels = (
    await github.paginate(github.rest.issues.listLabelsOnIssue, {
      owner,
      repo,
      issue_number: pr.number,
    })
  ).map((l) => l.name);

  // Remove stale size labels.
  for (const label of currentLabels) {
    if (label.startsWith("size/") && label !== sizeLabel) {
      console.log(`Removing stale label: ${label}`);
      await github.rest.issues
        .removeLabel({ owner, repo, issue_number: pr.number, name: label })
        .catch((e) => console.warn(`Failed to remove label ${label}: ${e.message}`));
    }
  }

  // Add the correct label, creating it on first use.
  if (!currentLabels.includes(sizeLabel)) {
    try {
      await github.rest.issues.getLabel({ owner, repo, name: sizeLabel });
    } catch (e) {
      if (e.status !== 404) throw e;
      console.log(`Creating label: ${sizeLabel}`);
      await github.rest.issues.createLabel({
        owner,
        repo,
        name: sizeLabel,
        color: "ededed",
        description: `Pull request size: ${sizeLabel.replace("size/", "")}`,
      });
    }
    await github.rest.issues.addLabels({
      owner,
      repo,
      issue_number: pr.number,
      labels: [sizeLabel],
    });
  }
};
