#!/usr/bin/env bash
# CodeBuild 프로젝트(maas-build) + CodePipeline(maas-pipeline) 을 생성한다.
# 멱등적이다 - 이미 있으면 update 를 시도한다.
#
# 이미 만들어져 있다고 가정하고 이 스크립트가 건드리지 않는 것들:
#   - GitHub 연결(CodeConnections), 아티팩트 버킷, 배포 버킷
#   - IAM 역할(maas-codedeploy-role/maas-codebuild-role/maas-codepipeline-role)
#   - EC2 인스턴스, CodeDeploy 애플리케이션/배포그룹, CodeDeploy 에이전트
#
# 사용법: ./scripts/create_pipeline.sh
#   필요: aws CLI, 계정 508139322599 / 리전 ap-northeast-2 에 대한 자격 증명.

set -euo pipefail

REGION="ap-northeast-2"
ACCOUNT_ID="508139322599"

CODEBUILD_PROJECT="maas-build"
PIPELINE_NAME="maas-pipeline"
ARTIFACT_BUCKET="maas-pipeline-508139322599"

CONNECTION_ARN="arn:aws:codeconnections:ap-northeast-2:508139322599:connection/e270b1df-ef5b-4f25-9417-520238e03816"
REPO_ID="sweeetgy-boop/maas-multilingual-agent"
BRANCH="main"

CODEBUILD_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/maas-codebuild-role"
CODEPIPELINE_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/maas-codepipeline-role"

CODEDEPLOY_APP="maas-api"
CODEDEPLOY_GROUP="maas-api-prod"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

# ── CodeBuild 프로젝트 ──────────────────────────────────────────────
echo "== CodeBuild 프로젝트: ${CODEBUILD_PROJECT} =="

EXISTING_PROJECT=$(aws codebuild batch-get-projects \
  --names "$CODEBUILD_PROJECT" --region "$REGION" \
  --query 'projects[0].name' --output text 2>/dev/null || echo "None")

CODEBUILD_ARGS=(
  --name "$CODEBUILD_PROJECT"
  --source "type=CODEPIPELINE"
  --artifacts "type=CODEPIPELINE"
  --environment "type=LINUX_CONTAINER,image=aws/codebuild/amazonlinux2-x86_64-standard:5.0,computeType=BUILD_GENERAL1_SMALL"
  --service-role "$CODEBUILD_ROLE_ARN"
  --region "$REGION"
)

if [ "$EXISTING_PROJECT" = "$CODEBUILD_PROJECT" ]; then
  echo "이미 존재 -> update-project"
  aws codebuild update-project "${CODEBUILD_ARGS[@]}" >/dev/null
else
  echo "없음 -> create-project"
  aws codebuild create-project "${CODEBUILD_ARGS[@]}" >/dev/null
fi
echo "OK"
echo

# ── CodePipeline ────────────────────────────────────────────────────
echo "== CodePipeline: ${PIPELINE_NAME} =="

PIPELINE_JSON="${TMP_DIR}/pipeline.json"
cat > "$PIPELINE_JSON" <<JSON
{
  "pipeline": {
    "name": "${PIPELINE_NAME}",
    "roleArn": "${CODEPIPELINE_ROLE_ARN}",
    "artifactStore": {
      "type": "S3",
      "location": "${ARTIFACT_BUCKET}"
    },
    "stages": [
      {
        "name": "Source",
        "actions": [
          {
            "name": "Source",
            "actionTypeId": {
              "category": "Source",
              "owner": "AWS",
              "provider": "CodeStarSourceConnection",
              "version": "1"
            },
            "configuration": {
              "ConnectionArn": "${CONNECTION_ARN}",
              "FullRepositoryId": "${REPO_ID}",
              "BranchName": "${BRANCH}"
            },
            "outputArtifacts": [{"name": "SourceOutput"}]
          }
        ]
      },
      {
        "name": "Build",
        "actions": [
          {
            "name": "Build",
            "actionTypeId": {
              "category": "Build",
              "owner": "AWS",
              "provider": "CodeBuild",
              "version": "1"
            },
            "configuration": {
              "ProjectName": "${CODEBUILD_PROJECT}"
            },
            "inputArtifacts": [{"name": "SourceOutput"}],
            "outputArtifacts": [{"name": "BuildOutput"}]
          }
        ]
      },
      {
        "name": "Deploy",
        "actions": [
          {
            "name": "Deploy",
            "actionTypeId": {
              "category": "Deploy",
              "owner": "AWS",
              "provider": "CodeDeploy",
              "version": "1"
            },
            "configuration": {
              "ApplicationName": "${CODEDEPLOY_APP}",
              "DeploymentGroupName": "${CODEDEPLOY_GROUP}"
            },
            "inputArtifacts": [{"name": "BuildOutput"}]
          }
        ]
      }
    ]
  }
}
JSON

if aws codepipeline get-pipeline --name "$PIPELINE_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "이미 존재 -> update-pipeline"
  aws codepipeline update-pipeline --cli-input-json "file://${PIPELINE_JSON}" --region "$REGION" >/dev/null
else
  echo "없음 -> create-pipeline"
  aws codepipeline create-pipeline --cli-input-json "file://${PIPELINE_JSON}" --region "$REGION" >/dev/null
fi
echo "OK"
echo

echo "완료: CodeBuild(${CODEBUILD_PROJECT}), CodePipeline(${PIPELINE_NAME})"
echo "CodeStarSourceConnection 이 PENDING 상태라면 콘솔에서 한 번 승인해야 파이프라인이 돈다:"
echo "  https://console.aws.amazon.com/codesuite/settings/connections?region=${REGION}"
