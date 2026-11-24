// Supervisor may only delegate. It has no data or payment tools.
permit(
  principal is AgentCore::IamEntity,
  action in [
    AgentCore::Action::"delegate___ask_intake_agent",
    AgentCore::Action::"delegate___ask_matching_agent",
    AgentCore::Action::"delegate___ask_payment_agent"
  ],
  resource == AgentCore::Gateway::"{GATEWAY_ARN}"
) when {
  (principal.id == "arn:aws:sts::{ACCOUNT}:assumed-role/{PREFIX}-supervisor-harness" ||
   principal.id like "arn:aws:sts::{ACCOUNT}:assumed-role/{PREFIX}-supervisor-harness/*")
};
