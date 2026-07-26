import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

/**
 * Staging bucket for falai-mcp.
 *
 * fal.ai's editing endpoints take image *URLs*, not uploads, so a local file
 * has to be reachable over HTTPS for the duration of one call. falai-mcp puts
 * the image here, hands fal a short-lived presigned GET, then deletes the
 * object as soon as the call returns.
 *
 * The lifecycle rule is the backstop for when that delete does not happen —
 * a crash, a network drop, a killed process. Nothing here is meant to persist.
 */
export class FalaiUploadsStack extends cdk.Stack {
  readonly bucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: cdk.StackProps) {
    super(scope, id, props);

    this.bucket = new s3.Bucket(this, 'FalaiUploadsBucket', {
      bucketName: 'nak-sandbox-falai-uploads',

      // Presigned URLs carry their own authorisation — the bucket itself
      // never needs to be public.
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,

      lifecycleRules: [
        {
          id: 'expire-after-24h',
          enabled: true,
          expiration: cdk.Duration.days(1),
          // A failed multipart upload leaves parts that are billed but
          // invisible in the object listing. Clear them on the same schedule.
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(1),
        },
      ],

      // Sandbox-only and holds nothing of value: tear it down cleanly.
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    new cdk.CfnOutput(this, 'FalaiUploadsBucketName', {
      value: this.bucket.bucketName,
      description: 'Set FALAI_BUCKET to this in meta-mcp config.toml',
    });
  }
}
