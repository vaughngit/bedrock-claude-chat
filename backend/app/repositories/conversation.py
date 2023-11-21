import json
import logging
import os
from datetime import datetime
from decimal import Decimal as decimal
from functools import wraps

import boto3
from app.repositories.common import (
    TABLE_NAME,
    RecordNotFoundError,
    _compose_bot_id,
    _compose_conv_id,
    _decompose_conv_id,
    _get_dynamodb_client,
    _get_table_client,
)
from app.repositories.model import (
    ContentModel,
    ConversationMetaModel,
    ConversationModel,
    MessageModel,
)
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)
sts_client = boto3.client("sts")


# def _get_table_client(user_id: str):
#     """Get a DynamoDB table client with row level access
#     Ref: https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_examples_dynamodb_items.html
#     """
#     if "AWS_EXECUTION_ENV" not in os.environ:
#         if DDB_ENDPOINT_URL:
#             # NOTE: This is for local development using DynamDB Local
#             dynamodb = boto3.resource(
#                 "dynamodb",
#                 endpoint_url=DDB_ENDPOINT_URL,
#                 aws_access_key_id="key",
#                 aws_secret_access_key="key",
#                 region_name="us-east-1",
#             )
#         else:
#             dynamodb = boto3.resource("dynamodb")
#         return dynamodb.Table(TABLE_NAME)

#     policy_document = {
#         "Statement": [
#             {
#                 "Effect": "Allow",
#                 "Action": [
#                     "dynamodb:BatchGetItem",
#                     "dynamodb:BatchWriteItem",
#                     "dynamodb:ConditionCheckItem",
#                     "dynamodb:DeleteItem",
#                     "dynamodb:DescribeTable",
#                     "dynamodb:GetItem",
#                     "dynamodb:GetRecords",
#                     "dynamodb:PutItem",
#                     "dynamodb:Query",
#                     "dynamodb:Scan",
#                     "dynamodb:UpdateItem",
#                 ],
#                 "Resource": [
#                     f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{TABLE_NAME}",
#                     f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{TABLE_NAME}/index/*",
#                 ],
#                 "Condition": {
#                     # Allow access to items with the same partition key as the user id
#                     "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"{user_id}*"]}
#                 },
#             }
#         ]
#     }
#     assumed_role_object = sts_client.assume_role(
#         RoleArn=TABLE_ACCESS_ROLE_ARN,
#         RoleSessionName="DynamoDBSession",
#         Policy=json.dumps(policy_document),
#     )
#     credentials = assumed_role_object["Credentials"]
#     dynamodb = boto3.resource(
#         "dynamodb",
#         region_name=REGION,
#         aws_access_key_id=credentials["AccessKeyId"],
#         aws_secret_access_key=credentials["SecretAccessKey"],
#         aws_session_token=credentials["SessionToken"],
#     )
#     table = dynamodb.Table(TABLE_NAME)
#     return table


def store_conversation(user_id: str, conversation: ConversationModel):
    logger.debug(f"Storing conversation: {conversation.model_dump_json()}")
    client = _get_dynamodb_client(user_id)

    transact_items = [
        {
            "Put": {
                "TableName": TABLE_NAME,
                "Item": {
                    "PK": user_id,
                    "SK": _compose_conv_id(user_id, conversation.id),
                    "Title": conversation.title,
                    "CreateTime": decimal(conversation.create_time),
                    "MessageMap": json.dumps(
                        {k: v.model_dump() for k, v in conversation.message_map.items()}
                    ),
                    "LastMessageId": conversation.last_message_id,
                    "BotId": conversation.bot_id,
                },
            }
        },
    ]
    if conversation.bot_id:
        transact_items.append(
            # Update `LastBotUsed`
            {
                "Update": {
                    "TableName": TABLE_NAME,
                    "Key": {
                        "PK": user_id,
                        "SK": _compose_bot_id(user_id, conversation.bot_id),
                    },
                    "UpdateExpression": "set LastBotUsed = :current_time",
                    "ExpressionAttributeValues": {
                        ":current_time": decimal(datetime.now().timestamp())
                    },
                }
            },
        )

    response = client.transact_write_items(TransactItems=transact_items)
    return response


def find_conversation_by_user_id(user_id: str) -> list[ConversationMetaModel]:
    logger.debug(f"Finding conversations for user: {user_id}")
    table = _get_table_client(user_id)
    response = table.query(
        KeyConditionExpression=Key("PK").eq(user_id)
        # NOTE: Need SK to fetch only conversations
        & Key("SK").begins_with(f"{user_id}#CONV#"),
        ScanIndexForward=False,
    )

    conversations = [
        ConversationMetaModel(
            id=_decompose_conv_id(item["SK"]),
            create_time=float(item["CreateTime"]),
            title=item["Title"],
            # NOTE: all message has the same model
            model=json.loads(item["MessageMap"]).popitem()[1]["model"],
            bot_id=item["BotId"],
        )
        for item in response["Items"]
    ]

    query_count = 1
    MAX_QUERY_COUNT = 5
    while "LastEvaluatedKey" in response:
        model = json.loads(response["Items"][0]["MessageMap"]).popitem()[1]["model"]
        # NOTE: max page size is 1MB
        # See: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Query.Pagination.html
        response = table.query(
            KeyConditionExpression=Key("PK").eq(user_id)
            # NOTE: Need SK to fetch only conversations
            & Key("SK").begins_with(f"{user_id}#CONV#"),
            ProjectionExpression="SK, CreateTime, Title",
            ScanIndexForward=False,
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        conversations.extend(
            [
                ConversationMetaModel(
                    id=_decompose_conv_id(item["SK"]),
                    create_time=float(item["CreateTime"]),
                    title=item["Title"],
                    model=model,
                    bot_id=item["BotId"],
                )
                for item in response["Items"]
            ]
        )
        query_count += 1
        if query_count > MAX_QUERY_COUNT:
            logger.warning(f"Query count exceeded {MAX_QUERY_COUNT}")
            break

    logger.debug(f"Found conversations: {conversations}")
    return conversations


def find_conversation_by_id(user_id: str, conversation_id: str) -> ConversationModel:
    logger.debug(f"Finding conversation: {conversation_id}")
    table = _get_table_client(user_id)
    response = table.query(
        IndexName="SKIndex",
        KeyConditionExpression=Key("SK").eq(_compose_conv_id(user_id, conversation_id)),
    )
    if len(response["Items"]) == 0:
        raise RecordNotFoundError(f"No conversation found with id: {conversation_id}")

    # NOTE: conversation is unique
    item = response["Items"][0]
    conv = ConversationModel(
        id=_decompose_conv_id(item["SK"]),
        create_time=float(item["CreateTime"]),
        title=item["Title"],
        message_map={
            k: MessageModel(
                role=v["role"],
                content=ContentModel(
                    content_type=v["content"]["content_type"],
                    body=v["content"]["body"],
                ),
                model=v["model"],
                children=v["children"],
                parent=v["parent"],
                create_time=float(v["create_time"]),
            )
            for k, v in json.loads(item["MessageMap"]).items()
        },
        last_message_id=item["LastMessageId"],
        bot_id=item["BotId"],
    )
    logger.debug(f"Found conversation: {conv}")
    return conv


def delete_conversation_by_id(user_id: str, conversation_id: str):
    logger.debug(f"Deleting conversation: {conversation_id}")
    table = _get_table_client(user_id)

    # Query the index
    response = table.query(
        IndexName="SKIndex",
        KeyConditionExpression=Key("SK").eq(_compose_conv_id(user_id, conversation_id)),
    )

    # Check if conversation exists
    if response["Items"]:
        user_id = response["Items"][0]["PK"]
        key = {
            "PK": user_id,
            "SK": _compose_conv_id(user_id, conversation_id),
        }
        delete_response = table.delete_item(Key=key)
        return delete_response
    else:
        raise RecordNotFoundError(f"No conversation found with id: {conversation_id}")


def delete_conversation_by_user_id(user_id: str):
    logger.debug(f"Deleting conversations for user: {user_id}")
    # First, find all conversations for the user
    conversations = find_conversation_by_user_id(user_id)
    if conversations:
        table = _get_table_client(user_id)
        responses = []
        for conversation in conversations:
            # Construct key to delete
            key = {
                "PK": user_id,
                "SK": _compose_conv_id(user_id, conversation.id),
            }
            response = table.delete_item(Key=key)
            responses.append(response)
        return responses
    else:
        raise RecordNotFoundError(f"No conversations found for user id: {user_id}")


def change_conversation_title(user_id: str, conversation_id: str, new_title: str):
    logger.debug(f"Changing conversation title: {conversation_id}")
    logger.debug(f"New title: {new_title}")
    table = _get_table_client(user_id)

    # First, we need to find the item using the GSI
    response = table.query(
        IndexName="SKIndex",
        KeyConditionExpression=Key("SK").eq(_compose_conv_id(user_id, conversation_id)),
    )

    items = response["Items"]
    if not items:
        raise RecordNotFoundError(f"No conversation found with id {conversation_id}")

    # We'll just update the first item in case there are multiple matches
    item = items[0]
    user_id = item["PK"]

    # Then, we update the item using its primary key
    response = table.update_item(
        Key={
            "PK": user_id,
            "SK": _compose_conv_id(user_id, conversation_id),
        },
        UpdateExpression="set Title=:t",
        ExpressionAttributeValues={":t": new_title},
        ReturnValues="UPDATED_NEW",
    )
    logger.debug(f"Updated conversation title response: {response}")

    return response
