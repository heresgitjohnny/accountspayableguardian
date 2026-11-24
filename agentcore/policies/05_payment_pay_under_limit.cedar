// Payment agent may auto-pay only at or under $2,000.00 (200000 cents).
permit(
  principal is AgentCore::IamEntity,
  action == AgentCore::Action::"agent-tools___pay_invoice",
  resource == AgentCore::Gateway::"{GATEWAY_ARN}"
) when {
  (principal.id == "arn:aws:sts::{ACCOUNT}:assumed-role/{PREFIX}-payment-harness" ||
   principal.id like "arn:aws:sts::{ACCOUNT}:assumed-role/{PREFIX}-payment-harness/*") &&
  context.input has amount_cents &&
  context.input.amount_cents <= 200000
};
