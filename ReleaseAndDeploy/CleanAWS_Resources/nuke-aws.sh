#!/bin/bash

# Set your target region
REGION="us-east-1"
echo "--- Starting cleanup in $REGION ---"

# 1. Terminate all EC2 Instances
echo "Terminating EC2 instances..."
INSTANCES=$(aws ec2 describe-instances --region $REGION --query 'Reservations[*].Instances[*].InstanceId' --output text)
if [ ! -z "$INSTANCES" ]; then
    aws ec2 terminate-instances --region $REGION --instance-ids $INSTANCES
    echo "Wait for instances to shut down..."
    aws ec2 wait instance-terminated --region $REGION --instance-ids $INSTANCES
fi

# 2. Delete all S3 Buckets (and their contents)
echo "Emptying and deleting S3 buckets..."
BUCKETS=$(aws s3api list-buckets --query 'Buckets[*].Name' --output text)
for bucket in $BUCKETS; do
    echo "Deleting $bucket..."
    aws s3 rb s3://$bucket --force 
done

# 3. Delete RDS Databases
echo "Deleting RDS instances..."
RDS_INSTANCES=$(aws rds describe-db-instances --region $REGION --query 'DBInstances[*].DBInstanceIdentifier' --output text)
for rds in $RDS_INSTANCES; do
    aws rds delete-db-instance --region $REGION --db-instance-identifier $rds --skip-final-snapshot
done

# 4. Delete Lambda Functions
echo "Deleting Lambda functions..."
LAMBDAS=$(aws lambda list-functions --region $REGION --query 'Functions[*].FunctionName' --output text)
for func in $LAMBDAS; do
    aws lambda delete-function --region $REGION --function-name $func
done

# 5. Release Elastic IPs
echo "Releasing Elastic IPs..."
ALLOC_IDS=$(aws ec2 describe-addresses --region $REGION --query 'Addresses[*].AllocationId' --output text)
for id in $ALLOC_IDS; do
    aws ec2 release-address --region $REGION --allocation-id $id
done

echo "--- Cleanup process initiated. Check the AWS Console for status. ---"