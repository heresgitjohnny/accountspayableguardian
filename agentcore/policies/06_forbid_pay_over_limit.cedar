// Belt and braces: nobody pays over the limit through the gateway. Forbid always wins.
forbid(
  principal,
  action == AgentCore::Action::"agent-tools___pay_invoice",
  resource == AgentCore::Gateway::"{GATEWAY_ARN}"
) unless {
  context.input has amount_cents &&
  context.input.amount_cents <= 200000
};
