#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import { AlexaAnnounceStack } from '../lib/alexa-announce-stack';
import { FalaiUploadsStack } from '../lib/falai-uploads-stack';

const deployEnv = process.env.NPM_ENVIRONMENT;
if (deployEnv !== 'sandbox' && deployEnv !== 'prod') {
  throw new Error(`NPM_ENVIRONMENT must be 'sandbox' or 'prod', got: '${deployEnv ?? ''}'`);
}

const sandboxAccountId = '975050268859';
const prodAccountId = '637423226886';
const accountId = deployEnv === 'prod' ? prodAccountId : sandboxAccountId;
const londonEnv = { env: { account: accountId, region: 'eu-west-2' } };

const app = new cdk.App();

// One throwaway staging bucket: a prod copy would be pure ceremony.
if (deployEnv === 'sandbox') {
  new FalaiUploadsStack(app, 'McpsFalaiUploadsStack', {
    ...londonEnv,
    description: 'Short-lived image staging bucket for falai-mcp (sandbox)',
  });
}

new AlexaAnnounceStack(app, 'McpsAlexaAnnounceStack', {
  ...londonEnv,
  deployEnv,
  description: `Alexa announcements via alexapy for alexa-mcp (${deployEnv})`,
});

const { version: infraVersion } = JSON.parse(fs.readFileSync('./version.json', 'utf-8'));
cdk.Tags.of(app).add('MH-Project', 'mcps');
cdk.Tags.of(app).add('MH-Version', infraVersion);
