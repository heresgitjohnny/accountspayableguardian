// Payment agent may always hand an invoice to a human.
permit(
  principal is AgentCore::IamEntity,
  action == AgentCore::Action::"agent-tools___request_approval",
  resource == AgentCore::Gateway::"{GATEWAY_ARN}"
) when {
  (principal.id == "arn:aws:sts::{ACCOUNT}:assumed-role/{PREFIX}-payment-harness" ||
   principal.id like "arn:aws:sts::{ACCOUNT}:assumed-role/{PREFIX}-payment-harness/*")
};
