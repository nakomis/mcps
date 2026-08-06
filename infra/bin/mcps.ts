#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import { FalaiUploadsStack } from '../lib/falai-uploads-stack';

// Sandbox only — this account holds one throwaway staging bucket, so the
// sandbox/prod split the other projects carry would be pure ceremony here.
const sandboxAccountId = '975050268859';
const londonEnv = { env: { account: sandboxAccountId, region: 'eu-west-2' } };

const app = new cdk.App();

new FalaiUploadsStack(app, 'McpsFalaiUploadsStack', {
  ...londonEnv,
  description: 'Short-lived image staging bucket for falai-mcp (sandbox)',
});

const { version: infraVersion } = JSON.parse(fs.readFileSync('./version.json', 'utf-8'));
cdk.Tags.of(app).add('MH-Project', 'mcps');
cdk.Tags.of(app).add('MH-Version', infraVersion);
