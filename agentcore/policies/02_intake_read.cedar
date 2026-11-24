// Intake agent: read the invoice only.
permit(
  principal is AgentCore::IamEntity,
  action == AgentCore::Action::"agent-tools___read_invoice",
  resource == AgentCore::Gateway::"{GATEWAY_ARN}"
) when {
  (principal.id == "arn:aws:sts::{ACCOUNT}:assumed-role/{PREFIX}-intake-harness" ||
   principal.id like "arn:aws:sts::{ACCOUNT}:assumed-role/{PREFIX}-intake-harness/*")
};
