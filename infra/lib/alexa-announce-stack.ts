import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { execSync } from 'child_process';
import { Construct } from 'constructs';
import * as fs from 'fs';
import * as path from 'path';

const LAMBDA_SRC = path.join(__dirname, '..', '..', 'alexa-mcp', 'lambda');

export interface AlexaAnnounceStackProps extends cdk.StackProps {
  deployEnv: 'sandbox' | 'prod';
}

/**
 * The Lambda behind alexa-mcp.
 *
 * Amazon has no public announcement API, so the function uses alexapy to drive
 * the private one the Alexa app uses. The device registration (a long-lived
 * refresh token) lives in the session secret and is written there by
 * alexa-mcp-login, never by this stack.
 */
export class AlexaAnnounceStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: AlexaAnnounceStackProps) {
    super(scope, id, props);

    // CDK only creates a placeholder ({"placeholder": "<random>"}); the Lambda
    // reports session_expired until alexa-mcp-login fills it in. Do NOT change
    // these generator props after the first deploy: CloudFormation would
    // regenerate the value and wipe the stored registration.
    const secret = new secretsmanager.Secret(this, 'AlexaSessionSecret', {
      secretName: 'alexa-announce/session',
      description: 'alexapy device registration for alexa-announce; written by alexa-mcp-login',
      generateSecretString: {
        secretStringTemplate: '{}',
        generateStringKey: 'placeholder',
      },
      // A teardown must not silently revoke the only copy of the registration.
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const fn = new lambda.Function(this, 'AlexaAnnounceFunction', {
      functionName: 'alexa-announce',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.X86_64,
      handler: 'alexa_announce.handler.handler',
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      environment: { SECRET_ID: secret.secretName },
      code: lambda.Code.fromAsset(LAMBDA_SRC, {
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          platform: 'linux/amd64',
          command: [
            'bash', '-c',
            'pip install -r requirements.txt -t /asset-output && cp -r alexa_announce /asset-output/',
          ],
          // Bundle with uv on the host so no Docker is needed. These are the
          // flags the 10 Oct 2026 spike proved on Lambda.
          local: {
            tryBundle(outputDir: string): boolean {
              try {
                execSync(
                  `uv pip install --quiet --target "${outputDir}" ` +
                    '--python-platform x86_64-manylinux_2_28 --python-version 3.12 ' +
                    '--only-binary :all: -r requirements.txt',
                  { cwd: LAMBDA_SRC, stdio: 'inherit' },
                );
                fs.cpSync(path.join(LAMBDA_SRC, 'alexa_announce'), path.join(outputDir, 'alexa_announce'), {
                  recursive: true,
                  filter: (src) => !src.includes('__pycache__'),
                });
                return true;
              } catch (err) {
                console.warn('uv bundling failed, falling back to Docker: ' + err);
                return false;
              }
            },
          },
        },
      }),
      logGroup: new logs.LogGroup(this, 'AlexaAnnounceLogs', {
        logGroupName: '/aws/lambda/alexa-announce',
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });

    secret.grantRead(fn);

    new cdk.CfnOutput(this, 'AlexaAnnounceFunctionName', { value: fn.functionName });
    new cdk.CfnOutput(this, 'AlexaAnnounceFunctionArn', { value: fn.functionArn });
    new cdk.CfnOutput(this, 'AlexaSessionSecretArn', {
      value: secret.secretArn,
      description: `Populate with: alexa-mcp-login <email> --profile ${props.deployEnv === 'prod' ? 'nakom.is' : 'nakom.is-sandbox'}`,
    });
  }
}
