// Matching agent: read invoice + look up PO. No write tools.
permit(
  principal is AgentCore::IamEntity,
  action in [
    AgentCore::Action::"agent-tools___read_invoice",
    AgentCore::Action::"agent-tools___lookup_po"
  ],
  resource == AgentCore::Gateway::"{GATEWAY_ARN}"
) when {
  (principal.id == "arn:aws:sts::{ACCOUNT}:assumed-role/{PREFIX}-matching-harness" ||
   principal.id like "arn:aws:sts::{ACCOUNT}:assumed-role/{PREFIX}-matching-harness/*")
};
