from modularodm import Q
from rest_framework import generics, permissions as drf_permissions
from rest_framework.exceptions import ValidationError, NotFound, PermissionDenied

from framework.auth.oauth_scopes import CoreScopes

from api.base import generic_bulk_views as bulk_views
from api.base import permissions as base_permissions
from api.base.filters import ODMFilterMixin
from api.base.views import JSONAPIBaseView
from api.base.views import BaseLinkedList
from api.base.views import LinkedNodesRelationship
from api.base.views import LinkedRegistrationsRelationship

from api.base.utils import get_object_or_error, is_bulk_request, get_user_auth
from api.collections.serializers import (
    CollectionSerializer,
    CollectionDetailSerializer,
    CollectionNodeLinkSerializer,
)
from api.nodes.serializers import NodeSerializer
from api.registrations.serializers import RegistrationSerializer

from api.nodes.permissions import (
    ContributorOrPublic,
    ReadOnlyIfRegistration,
    ContributorOrPublicForPointers,
)

from website.exceptions import NodeStateError
from osf.models import Collection, NodeRelation
from website.util.permissions import ADMIN


class CollectionMixin(object):
    """Mixin with convenience methods for retrieving the current collection based on the
    current URL. By default, fetches the current node based on the collection_id kwarg.
    """

    serializer_class = CollectionSerializer
    node_lookup_url_kwarg = 'collection_id'

    def get_node(self, check_object_permissions=True):
        node = get_object_or_error(
            Collection,
            self.kwargs[self.node_lookup_url_kwarg],
            display_name='collection'
        )
        # Nodes that are folders/collections are treated as a separate resource, so if the client
        # requests a non-collection through a collection endpoint, we return a 404
        if not node.is_collection:
            raise NotFound
        # May raise a permission denied
        if check_object_permissions:
            self.check_object_permissions(self.request, node)
        return node


class CollectionList(JSONAPIBaseView, bulk_views.BulkUpdateJSONAPIView, bulk_views.BulkDestroyJSONAPIView, bulk_views.ListBulkCreateJSONAPIView, ODMFilterMixin):
    """Organizer Collections organize projects and components. *Writeable*.

    Paginated list of Project Organizer Collections ordered by their `date_modified`.
    Each resource contains the full representation of the project organizer collection, meaning additional
    requests to an individual Organizer Collection's detail view are not necessary.

    The Project Organizer is a tool to allow the user to make Collections of projects, components, and registrations
    for whatever purpose the user might want to organize them. They make node_links to any Node that a user has
    read access to. Collections through this API do not nest. Currently Collections are private to any individual user,
    though that could change one day.

    ##Collection Attributes

    OSF Organizer Collection entities have the "nodes" `type`.

        name           type               description
        =================================================================================
        title          string             title of Organizer Collection
        date_created   iso8601 timestamp  timestamp that the collection was created
        date_modified  iso8601 timestamp  timestamp when the collection was last updated


    ##Links

    See the [JSON-API spec regarding pagination](http://jsonapi.org/format/1.0/#fetching-pagination).

    ##Actions

    ###Creating New Organizer Collections

        Method:        POST
        URL:           /links/self
        Query Params:  <none>
        Body (JSON):   {
                         "data": {
                           "type": "collections", # required
                           "attributes": {
                             "title":       {title},          # required
                           }
                         }
                        }
        Success:       201 CREATED + collection representation

    New Organizer Collections are created by issuing a POST request to this endpoint.  The `title` field is
    mandatory. All other fields not listed above will be ignored.  If the Organizer Collection creation is successful
    the API will return a 201 response with the representation of the new node in the body.
    For the new Collection's canonical URL, see the `/links/self` field of the response.

    ##Query Params

    + `page=<Int>` -- page number of results to view, default 1

    + `filter[<fieldname>]=<Str>` -- fields and values to filter the search results on.

    Organizer Collections may be filtered by their `title`, which is a string field and will be filtered using simple
    substring matching.

    #This Request/Response

    """
    permission_classes = (
        drf_permissions.IsAuthenticatedOrReadOnly,
        base_permissions.TokenHasScope,
    )

    required_read_scopes = [CoreScopes.ORGANIZER_COLLECTIONS_BASE_READ]
    required_write_scopes = [CoreScopes.ORGANIZER_COLLECTIONS_BASE_WRITE]

    serializer_class = CollectionSerializer
    view_category = 'collections'
    view_name = 'collection-list'
    model_class = Collection

    ordering = ('-date_modified', )  # default ordering

    # overrides ODMFilterMixin
    def get_default_odm_query(self):
        base_query = (
            Q('is_deleted', 'ne', True)
        )
        user = self.request.user
        if not user.is_anonymous:
            permission_query = Q('creator', 'eq', user)
        else:
            permission_query = Q('is_public', 'eq', True)
        query = base_query & permission_query
        return query

    # overrides ListBulkCreateJSONAPIView, BulkUpdateJSONAPIView
    def get_queryset(self):
        # For bulk requests, queryset is formed from request body.
        if is_bulk_request(self.request):
            query = Q('_id', 'in', [coll['id'] for coll in self.request.data])

            auth = get_user_auth(self.request)
            collections = Collection.find(query)
            for collection in collections:
                if not collection.can_edit(auth):
                    raise PermissionDenied
            return collections
        else:
            query = self.get_query_from_request()
            return Collection.find(query)

    # overrides ListBulkCreateJSONAPIView, BulkUpdateJSONAPIView, BulkDestroyJSONAPIView
    def get_serializer_class(self):
        """
        Use CollectionDetailSerializer which requires 'id'
        """
        if self.request.method in ('PUT', 'PATCH', 'DELETE'):
            return CollectionDetailSerializer
        else:
            return CollectionSerializer

    # overrides ListBulkCreateJSONAPIView
    def perform_create(self, serializer):
        """Create a node.

        :param serializer:
        """
        # On creation, make sure that current user is the creator
        user = self.request.user
        serializer.save(creator=user)

    # overrides BulkDestroyJSONAPIView
    def allow_bulk_destroy_resources(self, user, resource_list):
        """User must have admin permissions to delete nodes."""
        for node in resource_list:
            if not node.has_permission(user, ADMIN):
                return False
        return True

    # Overrides BulkDestroyJSONAPIView
    def perform_destroy(self, instance):
        auth = get_user_auth(self.request)
        try:
            instance.remove_node(auth=auth)
        except NodeStateError as err:
            raise ValidationError(err.message)
        instance.save()


class CollectionDetail(JSONAPIBaseView, generics.RetrieveUpdateDestroyAPIView, CollectionMixin):
    """Details about Organizer Collections. *Writeable*.

    The Project Organizer is a tool to allow the user to make Collections of projects, components, and registrations
    for whatever purpose the user might want to organize them. They make node_links to any Node that a user has
    read access to. Collections through this API do not nest. Currently Collections are private to any individual user,
    though that could change one day.

    ##Collection Attributes

    OSF Organizer Collection entities have the "nodes" `type`.

        name           type               description
        =================================================================================
        title          string             title of Organizer Collection
        date_created   iso8601 timestamp  timestamp that the collection was created
        date_modified  iso8601 timestamp  timestamp when the collection was last updated

    ##Relationships

    ###Node links

    Node links are pointers or aliases to nodes. This relationship lists all of the nodes that the Organizer Collection
    is pointing to. New node links can be created with this collection.

    ##Links

        self:  the canonical api endpoint of this node
        html:  this node's page on the OSF website

    ##Actions

    ###Update

        Method:        PUT / PATCH
        URL:           /links/self
        Query Params:  <none>
        Body (JSON):   {
                         "data": {
                           "type": "nodes",   # required
                           "id":   {node_id}, # required
                           "attributes": {
                             "title":       {title},          # mandatory
                           }
                         }
                       }
        Success:       200 OK + node representation

    To update an Organizer Collection, issue either a PUT or a PATCH request against the `/links/self` URL.
    The `title` field is mandatory if you PUT and optional if you PATCH, though there's no reason to PATCH if you aren't
    changing the name. Non-string values will be accepted and stringified, but we make no promises about the
    stringification output.  So don't do that.

    ###Delete

        Method:   DELETE
        URL:      /links/self
        Params:   <none>
        Success:  204 No Content

    To delete a node, issue a DELETE request against `/links/self`.  A successful delete will return a 204 No Content
    response. Attempting to delete a node you do not own will result in a 403 Forbidden.

    ##Query Params

    *None*.

    #This Request/Response

    """
    permission_classes = (
        drf_permissions.IsAuthenticatedOrReadOnly,
        ContributorOrPublic,
        base_permissions.TokenHasScope,
    )

    required_read_scopes = [CoreScopes.ORGANIZER_COLLECTIONS_BASE_READ]
    required_write_scopes = [CoreScopes.ORGANIZER_COLLECTIONS_BASE_WRITE]

    serializer_class = CollectionDetailSerializer
    view_category = 'collections'
    view_name = 'collection-detail'

    # overrides RetrieveUpdateDestroyAPIView
    def get_object(self):
        return self.get_node()

    # overrides RetrieveUpdateDestroyAPIView
    def perform_destroy(self, instance):
        auth = get_user_auth(self.request)
        node = self.get_object()
        try:
            node.remove_node(auth=auth)
        except NodeStateError as err:
            raise ValidationError(err.message)
        node.save()


class LinkedNodesList(BaseLinkedList, CollectionMixin):
    """List of nodes linked to this node. *Read-only*.

    Linked nodes are the project/component nodes pointed to by node links. This view will probably replace node_links in the near future.

    <!--- Copied Spiel from NodeDetail -->

    On the front end, nodes are considered 'projects' or 'components'. The difference between a project and a component
    is that a project is the top-level node, and components are children of the project. There is also a [category
    field](/v2/#osf-node-categories) that includes 'project' as an option. The categorization essentially determines
    which icon is displayed by the node in the front-end UI and helps with search organization. Top-level nodes may have
    a category other than project, and children nodes may have a category of project.

    ##Linked Node Attributes

    <!--- Copied Attributes from NodeDetail -->

    OSF Node entities have the "nodes" `type`.

        name           type               description
        =================================================================================
        title          string             title of project or component
        description    string             description of the node
        category       string             node category, must be one of the allowed values
        date_created   iso8601 timestamp  timestamp that the node was created
        date_modified  iso8601 timestamp  timestamp when the node was last updated
        tags           array of strings   list of tags that describe the node
        registration   boolean            is this is a registration?
        collection     boolean            is this node a collection of other nodes?
        public         boolean            has this node been made publicly-visible?

    ##Links

    See the [JSON-API spec regarding pagination](http://jsonapi.org/format/1.0/#fetching-pagination).

    ##Query Params

    + `page=<Int>` -- page number of results to view, default 1

    + `filter[<fieldname>]=<Str>` -- fields and values to filter the search results on.

    Nodes may be filtered by their `title`, `category`, `description`, `public`, `registration`, or `tags`.  `title`,
    `description`, and `category` are string fields and will be filtered using simple substring matching.  `public` and
    `registration` are booleans, and can be filtered using truthy values, such as `true`, `false`, `0`, or `1`.  Note
    that quoting `true` or `false` in the query will cause the match to fail regardless.  `tags` is an array of simple strings.

    #This Request/Response
    """
    serializer_class = NodeSerializer
    view_category = 'collections'
    view_name = 'linked-nodes'

    ordering = ('-date_modified',)

    def get_queryset(self):
        return super(LinkedNodesList, self).get_queryset().exclude(type='osf.registration')

    # overrides APIView
    def get_parser_context(self, http_request):
        """
        Tells parser that we are creating a relationship
        """
        res = super(LinkedNodesList, self).get_parser_context(http_request)
        res['is_relationship'] = True
        return res


class LinkedRegistrationsList(BaseLinkedList, CollectionMixin):
    """List of registrations linked to this node. *Read-only*.

    Linked registrations are the registration nodes pointed to by node links.

    <!--- Copied Spiel from RegistrationDetail -->
    Registrations are read-only snapshots of a project. This view shows details about the given registration.

    Each resource contains the full representation of the registration, meaning additional requests to an individual
    registration's detail view are not necessary. A withdrawn registration will display a limited subset of information,
    namely, title, description, date_created, registration, withdrawn, date_registered, withdrawal_justification, and
    registration supplement. All other fields will be displayed as null. Additionally, the only relationships permitted
    to be accessed for a withdrawn registration are the contributors - other relationships will return a 403.

    ##Linked Registration Attributes

    <!--- Copied Attributes from RegistrationDetail -->

    Registrations have the "registrations" `type`.

        name                            type               description
        =======================================================================================================
        title                           string             title of the registered project or component
        description                     string             description of the registered node
        category                        string             bode category, must be one of the allowed values
        date_created                    iso8601 timestamp  timestamp that the node was created
        date_modified                   iso8601 timestamp  timestamp when the node was last updated
        tags                            array of strings   list of tags that describe the registered node
        current_user_can_comment        boolean            Whether the current user is allowed to post comments
        current_user_permissions        array of strings   list of strings representing the permissions for the current user on this node
        fork                            boolean            is this project a fork?
        registration                    boolean            has this project been registered? (always true - may be deprecated in future versions)
        collection                      boolean            is this registered node a collection? (always false - may be deprecated in future versions)
        node_license                    object             details of the license applied to the node
        year                            string             date range of the license
        copyright_holders               array of strings   holders of the applied license
        public                          boolean            has this registration been made publicly-visible?
        withdrawn                       boolean            has this registration been withdrawn?
        date_registered                 iso8601 timestamp  timestamp that the registration was created
        embargo_end_date                iso8601 timestamp  when the embargo on this registration will be lifted (if applicable)
        withdrawal_justification        string             reasons for withdrawing the registration
        pending_withdrawal              boolean            is this registration pending withdrawal?
        pending_withdrawal_approval     boolean            is this registration pending approval?
        pending_embargo_approval        boolean            is the associated Embargo awaiting approval by project admins?
        registered_meta                 dictionary         registration supplementary information
        registration_supplement         string             registration template

    ##Links

    See the [JSON-API spec regarding pagination](http://jsonapi.org/format/1.0/#fetching-pagination).

    ##Query Params

    + `page=<Int>` -- page number of results to view, default 1

    + `filter[<fieldname>]=<Str>` -- fields and values to filter the search results on.

    Nodes may be filtered by their `title`, `category`, `description`, `public`, `registration`, or `tags`.  `title`,
    `description`, and `category` are string fields and will be filtered using simple substring matching.  `public` and
    `registration` are booleans, and can be filtered using truthy values, such as `true`, `false`, `0`, or `1`.  Note
    that quoting `true` or `false` in the query will cause the match to fail regardless.  `tags` is an array of simple strings.

    #This Request/Response
    """
    serializer_class = RegistrationSerializer
    view_category = 'collections'
    view_name = 'linked-registrations'

    ordering = ('-date_modified',)

    def get_queryset(self):
        return super(LinkedRegistrationsList, self).get_queryset().filter(type='osf.registration')

    # overrides APIView
    def get_parser_context(self, http_request):
        """
        Tells parser that we are creating a relationship
        """
        res = super(LinkedRegistrationsList, self).get_parser_context(http_request)
        res['is_relationship'] = True
        return res


class NodeLinksList(JSONAPIBaseView, bulk_views.BulkDestroyJSONAPIView, bulk_views.ListBulkCreateJSONAPIView, CollectionMixin):
    """Node Links to other nodes. *Writeable*.

    # Deprecated

    The use of /collections/ID/node_links/ is deprecated in favor of linked_nodes, linked_registrations or a combination of both.

    ## Known Issue

    Linked nodes of type 'registrations' will be returned with an error 'Not found.' in the {embeds: target_node: {}} object.


    Node Links act as pointers to other nodes. Unlike Forks, they are not copies of nodes;
    Node Links are a direct reference to the node that they point to.

    ##Node Link Attributes

    *None*

    ##Links

    See the [JSON-API spec regarding pagination](http://jsonapi.org/format/1.0/#fetching-pagination).

    ##Actions

    ###Create
        Method:        POST
        URL:           /links/self
        Query Params:  <none>
        Body (JSON):   {
                         "data": {
                           "type": "node_links", # required
                         },
                         'relationships': {
                            'target_node': {
                                'data': {
                                    'type': 'nodes',
                                    'id': '<node_id>'
                                }
                            }
                        }


    ##Query Params

    + `page=<Int>` -- page number of results to view, default 1

    + `filter[<fieldname>]=<Str>` -- fields and values to filter the search results on.

    #This Request/Response
    """
    permission_classes = (
        drf_permissions.IsAuthenticatedOrReadOnly,
        ContributorOrPublic,
        ReadOnlyIfRegistration,
        base_permissions.TokenHasScope,
    )

    required_read_scopes = [CoreScopes.NODE_LINKS_READ]
    required_write_scopes = [CoreScopes.NODE_LINKS_WRITE]

    serializer_class = CollectionNodeLinkSerializer
    view_category = 'collections'
    view_name = 'node-pointers'
    model_class = NodeRelation

    ordering = ('-date_modified',)

    def get_queryset(self):
        return self.get_node().node_relations.select_related('child').filter(child__is_deleted=False).exclude(child__type='osf.collection')

    # Overrides BulkDestroyJSONAPIView
    def perform_destroy(self, instance):
        auth = get_user_auth(self.request)
        node = self.get_node()
        try:
            node.rm_pointer(instance, auth=auth)
        except ValueError as err:  # pointer doesn't belong to node
            raise ValidationError(err.message)
        node.save()

    # overrides ListCreateAPIView
    def get_parser_context(self, http_request):
        """
        Tells parser that we are creating a relationship
        """
        res = super(NodeLinksList, self).get_parser_context(http_request)
        res['is_relationship'] = True
        return res


class NodeLinksDetail(JSONAPIBaseView, generics.RetrieveDestroyAPIView, CollectionMixin):
    """Node Link details. *Writeable*.

    Node Links act as pointers to other nodes. Unlike Forks, they are not copies of nodes;
    Node Links are a direct reference to the node that they point to.

    ##Attributes

    *None*

    ##Relationships

    ##Links

    self:  the canonical api endpoint of this node

    ##Actions

    ###Delete

        Method:   DELETE
        URL:      /links/self
        Params:   <none>
        Success:  204 No Content

    To delete a node_link, issue a DELETE request against `/links/self`.  A successful delete will return a 204 No Content
    response. Attempting to delete a node you do not own will result in a 403 Forbidden.

    ##Query Params

    *None*.

    #This Request/Response
    """
    permission_classes = (
        ContributorOrPublicForPointers,
        drf_permissions.IsAuthenticatedOrReadOnly,
        base_permissions.TokenHasScope,
        ReadOnlyIfRegistration,
    )

    required_read_scopes = [CoreScopes.NODE_LINKS_READ]
    required_write_scopes = [CoreScopes.NODE_LINKS_WRITE]

    serializer_class = CollectionNodeLinkSerializer
    view_category = 'nodes'
    view_name = 'node-pointer-detail'

    # overrides RetrieveAPIView
    def get_object(self):
        node_link_lookup_url_kwarg = 'node_link_id'
        node_link = get_object_or_error(
            NodeRelation,
            self.kwargs[node_link_lookup_url_kwarg],
            'node link'
        )
        # May raise a permission denied
        self.kwargs['node_id'] = self.kwargs['collection_id']
        self.check_object_permissions(self.request, node_link)
        return node_link

    # overrides DestroyAPIView
    def perform_destroy(self, instance):
        auth = get_user_auth(self.request)
        node = self.get_node()
        pointer = self.get_object()
        try:
            node.rm_pointer(pointer, auth=auth)
        except ValueError as err:  # pointer doesn't belong to node
            raise ValidationError(err.message)
        node.save()


class CollectionLinkedNodesRelationship(LinkedNodesRelationship, CollectionMixin):
    """ Relationship Endpoint for Collection -> Linked Node relationships

    Used to set, remove, update and retrieve the ids of the linked nodes attached to this collection. For each id, there
    exists a node link that contains that node.

    ##Actions

    ###Create

        Method:        POST
        URL:           /links/self
        Query Params:  <none>
        Body (JSON):   {
                         "data": [{
                           "type": "linked_nodes",   # required
                           "id": <node_id>   # required
                         }]
                       }
        Success:       201

    This requires both edit permission on the collection, and for the user that is
    making the request to be able to read the nodes requested. Data can be contain any number of
    node identifiers. This will create a node_link for all node_ids in the request that
    do not currently have a corresponding node_link in this collection.

    ###Update

        Method:        PUT || PATCH
        URL:           /links/self
        Query Params:  <none>
        Body (JSON):   {
                         "data": [{
                           "type": "linked_nodes",   # required
                           "id": <node_id>   # required
                         }]
                       }
        Success:       200

    This requires both edit permission on the collection, and for the user that is
    making the request to be able to read the nodes requested. Data can be contain any number of
    node identifiers. This will replace the contents of the node_links for this collection with
    the contents of the request. It will delete all node links that don't have a node_id in the data
    array, create node links for the node_ids that don't currently have a node id, and do nothing
    for node_ids that already have a corresponding node_link. This means a update request with
    {"data": []} will remove all node_links in this collection

    ###Destroy

        Method:        DELETE
        URL:           /links/self
        Query Params:  <none>
        Body (JSON):   {
                         "data": [{
                           "type": "linked_nodes",   # required
                           "id": <node_id>   # required
                         }]
                       }
        Success:       204

    This requires edit permission on the node. This will delete any node_links that have a
    corresponding node_id in the request.
    """

    view_category = 'collections'
    view_name = 'collection-node-pointer-relationship'

class CollectionLinkedRegistrationsRelationship(LinkedRegistrationsRelationship, CollectionMixin):
    """ Relationship Endpoint for Collection -> Linked Registration relationships

    Used to set, remove, update and retrieve the ids of the linked registrations attached to this collection. For each id, there
    exists a node link that contains that node.

    ##Actions

    ###Create

        Method:        POST
        URL:           /links/self
        Query Params:  <none>
        Body (JSON):   {
                         "data": [{
                           "type": "linked_registrations",   # required
                           "id": <node_id>   # required
                         }]
                       }
        Success:       201

    This requires both edit permission on the collection, and for the user that is
    making the request to be able to read the registrations requested. Data can contain any number of
    node identifiers. This will create a node_link for all node_ids in the request that
    do not currently have a corresponding node_link in this collection.

    ###Update

        Method:        PUT || PATCH
        URL:           /links/self
        Query Params:  <none>
        Body (JSON):   {
                         "data": [{
                           "type": "linked_regisrations",   # required
                           "id": <node_id>   # required
                         }]
                       }
        Success:       200

    This requires both edit permission on the collection, and for the user that is
    making the request to be able to read the registrations requested. Data can contain any number of
    node identifiers. This will replace the contents of the node_links for this collection with
    the contents of the request. It will delete all node links that don't have a node_id in the data
    array, create node links for the node_ids that don't currently have a node id, and do nothing
    for node_ids that already have a corresponding node_link. This means a update request with
    {"data": []} will remove all node_links in this collection

    ###Destroy

        Method:        DELETE
        URL:           /links/self
        Query Params:  <none>
        Body (JSON):   {
                         "data": [{
                           "type": "linked_registrations",   # required
                           "id": <node_id>   # required
                         }]
                       }
        Success:       204

    This requires edit permission on the node. This will delete any node_links that have a
    corresponding node_id in the request.
    """

    view_category = 'collections'
    view_name = 'collection-registration-pointer-relationship'
