"""The complete list of provider calls this deployment is able to make.

A credential that can be used to make any request is a credential that has been
handed over. So the model-facing runtime never names a URL, a method, a header
or a host: it names one of the operations below, with arguments that are checked
against the shapes declared here, and the privileged side builds the request.

The table is the authority. It lives with the broker, not with the caller, so a
runtime that asked for something outside it is refused rather than trusted — and
adding a capability is a change to this file, reviewed like any other.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

#: Where each provider lives. Never assembled from an argument.
PROVIDER_BASES: Mapping[str, str] = {
    "trello": "https://api.trello.com/1",
    "ghl": "https://services.leadconnectorhq.com",
    "rentcast": "https://api.rentcast.io",
}

#: How a credential reaches the provider. The broker fills these in; the shapes
#: are declared here so no operation can invent an authorization scheme.
AUTH_QUERY = "query"
AUTH_BEARER = "bearer"
AUTH_HEADER = "header"

#: Argument shapes. An argument that does not match its declared shape is
#: refused before anything is built, so a path can never be steered by input.
SHAPES: Mapping[str, re.Pattern[str]] = {
    "id": re.compile(r"[A-Za-z0-9_-]{1,64}"),
    "text": re.compile(r"[^\x00-\x1f]{0,2000}"),
    "short_text": re.compile(r"[^\x00-\x1f]{0,256}"),
    "csv_ids": re.compile(r"[A-Za-z0-9_-]{1,64}(?:,[A-Za-z0-9_-]{1,64}){0,49}"),
    "number": re.compile(r"-?[0-9]{1,12}(?:\.[0-9]{1,6})?"),
    "flag": re.compile(r"true|false"),
    "iso_date": re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9:.+Z-]{1,20})?"),
    "phone": re.compile(r"\+[0-9]{7,15}"),
    "endpoint": re.compile(r"/v1/[A-Za-z0-9/_-]{1,64}"),
}


@dataclass(frozen=True, slots=True)
class Operation:
    """One thing this deployment can ask a provider to do, and nothing more."""

    provider: str
    method: str
    #: A path template. Only the placeholders below are substituted, and each
    #: substituted value has already matched its declared shape.
    path: str
    #: name -> shape, for values that go into the path.
    path_args: Mapping[str, str] = field(default_factory=dict)
    #: name -> shape, for values that go into the query string.
    query_args: Mapping[str, str] = field(default_factory=dict)
    #: name -> shape, for values that go into a JSON body.
    body_args: Mapping[str, str] = field(default_factory=dict)
    #: Arguments that must be present. Everything else is optional.
    required: tuple[str, ...] = ()
    #: Whether the effect is freely reversible. A consequence operation still
    #: needs the caller's own approval; this marks it for the audit receipt.
    consequence: bool = False
    #: How the credential is attached.
    auth: str = AUTH_QUERY

    def argument_shapes(self) -> dict[str, str]:
        return {**self.path_args, **self.query_args, **self.body_args}


#: Every operation, by its stable identifier. Identifiers are part of the
#: contract with the runtime and with stored workflows, so they do not change
#: meaning once published.
OPERATIONS: Mapping[str, Operation] = {
    # -- Trello ---------------------------------------------------------
    "trello.get_card": Operation(
        provider="trello",
        method="GET",
        path="/cards/{card_id}",
        path_args={"card_id": "id"},
        query_args={"fields": "short_text", "customFieldItems": "flag"},
        required=("card_id",),
    ),
    "trello.list_board_cards": Operation(
        provider="trello",
        method="GET",
        path="/boards/{board_id}/cards",
        path_args={"board_id": "id"},
        query_args={
            "fields": "short_text",
            "customFieldItems": "flag",
            "limit": "number",
            "before": "id",
            "since": "id",
        },
        required=("board_id",),
    ),
    "trello.list_board_lists": Operation(
        provider="trello",
        method="GET",
        path="/boards/{board_id}/lists",
        path_args={"board_id": "id"},
        query_args={"fields": "short_text"},
        required=("board_id",),
    ),
    "trello.list_cards_in_list": Operation(
        provider="trello",
        method="GET",
        path="/lists/{list_id}/cards",
        path_args={"list_id": "id"},
        query_args={"fields": "short_text", "limit": "number", "before": "id"},
        required=("list_id",),
    ),
    "trello.create_card": Operation(
        provider="trello",
        method="POST",
        path="/cards",
        query_args={
            "idList": "id",
            "name": "text",
            "desc": "text",
            "due": "iso_date",
            "idLabels": "csv_ids",
        },
        required=("idList",),
    ),
    "trello.update_card": Operation(
        provider="trello",
        method="PUT",
        path="/cards/{card_id}",
        path_args={"card_id": "id"},
        query_args={
            "name": "text",
            "desc": "text",
            "due": "iso_date",
            "dueComplete": "flag",
            "idLabels": "csv_ids",
            "idList": "id",
            "closed": "flag",
        },
        required=("card_id",),
    ),
    "trello.set_custom_field": Operation(
        provider="trello",
        method="PUT",
        path="/cards/{card_id}/customField/{field_id}/item",
        path_args={"card_id": "id", "field_id": "id"},
        body_args={"value": "text"},
        required=("card_id", "field_id"),
    ),
    # -- GoHighLevel ----------------------------------------------------
    "ghl.get_contact": Operation(
        provider="ghl",
        method="GET",
        path="/contacts/{contact_id}",
        path_args={"contact_id": "id"},
        required=("contact_id",),
        auth=AUTH_BEARER,
    ),
    "ghl.search_conversations": Operation(
        provider="ghl",
        method="GET",
        path="/conversations/search",
        query_args={"contactId": "id", "limit": "number"},
        required=("contactId",),
        auth=AUTH_BEARER,
    ),
    "ghl.get_message": Operation(
        provider="ghl",
        method="GET",
        path="/conversations/{conversation_id}/messages/{message_id}",
        path_args={"conversation_id": "id", "message_id": "id"},
        required=("conversation_id", "message_id"),
        auth=AUTH_BEARER,
    ),
    "ghl.send_sms": Operation(
        provider="ghl",
        method="POST",
        path="/conversations/messages",
        body_args={"type": "short_text", "contactId": "id", "message": "text", "toNumber": "phone"},
        required=("type", "contactId", "message", "toNumber"),
        consequence=True,
        auth=AUTH_BEARER,
    ),
    # -- RentCast -------------------------------------------------------
    "rentcast.fetch": Operation(
        provider="rentcast",
        method="GET",
        path="{endpoint}",
        path_args={"endpoint": "endpoint"},
        query_args={
            "address": "text",
            "latitude": "number",
            "longitude": "number",
            "propertyType": "short_text",
            "bedrooms": "number",
            "bathrooms": "number",
            "squareFootage": "number",
            "limit": "number",
        },
        required=("endpoint",),
        auth=AUTH_HEADER,
    ),
}

#: The credential each provider needs, in the order it is applied.
PROVIDER_CREDENTIALS: Mapping[str, tuple[tuple[str, str], ...]] = {
    # Trello wants both halves in the query string, as its API requires.
    "trello": (("api_key", "key"), ("token", "token")),
    "ghl": (("private_token", "Authorization"),),
    "rentcast": (("api_key", "X-Api-Key"),),
}

#: Credentials that identify the integration rather than a person.
#:
#: Trello's API key says which application is calling; the token says who is
#: calling. Requiring a grant to use the application key would be requiring
#: permission to be this product, which is not a permission anybody grants. The
#: token is the one that carries an identity, and that one needs a grant when
#: it is the business's rather than the person's.
APPLICATION_CREDENTIALS: Mapping[str, frozenset[str]] = {
    "trello": frozenset({"api_key"}),
    "ghl": frozenset(),
    "rentcast": frozenset(),
}


def known(operation: object) -> Operation:
    """Look one operation up, or refuse. There is no default and no wildcard."""

    if type(operation) is not str or operation not in OPERATIONS:
        raise KeyError("unknown operation")
    return OPERATIONS[operation]


__all__ = [
    "AUTH_BEARER",
    "AUTH_HEADER",
    "AUTH_QUERY",
    "OPERATIONS",
    "PROVIDER_BASES",
    "APPLICATION_CREDENTIALS",
    "PROVIDER_CREDENTIALS",
    "SHAPES",
    "Operation",
    "known",
]
