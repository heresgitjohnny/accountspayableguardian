#!/usr/bin/env python3
import os

import aws_cdk as cdk

from infra.stack import ApGuardianStack

app = cdk.App()
ApGuardianStack(
    app, "ApGuardianStack",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
                        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1")),
    description="AP Guardian - accounts payable agent with layered boundaries",
)
app.synth()
