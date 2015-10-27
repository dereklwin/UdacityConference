#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


from datetime import datetime
import json
import os
import time

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import urlfetch
from google.appengine.ext import ndb
from google.appengine.api import memcache
from google.appengine.api import taskqueue

from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import TeeShirtSize
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForm
from models import ConferenceQueryForms
from models import BooleanMessage
from models import ConflictException
from models import StringMessage
from models import Speaker
from models import SpeakerForm
from models import Session
from models import SessionForm
from models import SessionForms
from models import SessionQueryForm
from models import SessionQueryForms
from models import TypeOfSession

from utils import getUserId

from settings import WEB_CLIENT_ID

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
MEMCACHE_SPEAKER_KEY = "SPEAKER_ANNOUNCEMENTS"

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": [ "Default", "Topic" ],
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<='
            }

FIELDS =    {
            'CITY': 'city',
            'TOPIC': 'topics',
            'MONTH': 'month',
            'MAX_ATTENDEES': 'maxAttendees',
            }

SESSION_FIELDS =    {
            'TYPE': 'typeOfSession',
            'TIME': 'startTime',
            }

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

SESSION_CONTAINER = endpoints.ResourceContainer(
    SessionForm,
    websafeConferenceKey=messages.StringField(1),
)

WISHLIST_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeSessionKey=messages.StringField(1),
)

SESSION_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
    sessionType = messages.StringField(2),
)

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

@endpoints.api( name='conference',
                version='v1',
                allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID],
                scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Conference objects - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf


    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        # both for data model & outbound Message
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
            setattr(request, "seatsAvailable", data["maxAttendees"])

        # make Profile Key from user ID
        p_key = ndb.Key(Profile, user_id)
        # allocate new Conference ID with Profile key as parent
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        # make Conference key from ID
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference, send email to organizer confirming
        # creation of Conference & return (modified) ConferenceForm
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )

        return request

    # Issues the query that is submitted by the user and returns the filtered object
    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters, FIELDS)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q


    # Converts user supplied filters to a format readable by app engine
    def _formatFilters(self, filters, fieldStruct):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = fieldStruct[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)

    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
            http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)

    @endpoints.method(ConferenceQueryForms, ConferenceForms,
            path='queryConferences',
            http_method='POST',
            name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)
    
         # for every conference in Conference Kind, copy the properties into 
         # conferene form and store all forms into ConferenceForms
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") \
            for conf in conferences]
        )

    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))

    @endpoints.method(message_types.VoidMessage, ConferenceForms, 
            path= 'getConferencesCreated',
            http_method='POST',
            name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        user_id = getUserId(user)
        # Create an instance of a Key for an id(user email) of a kind(Profile) 
        p_key = ndb.Key(Profile, user_id)
        # query conferences with ancestor user
        conferences = Conference.query(ancestor=p_key)
        # get the user profile and display name
        prof = p_key.get()
        displayName = getattr(prof, 'displayName')
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, displayName) for conf in conferences]
        )

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""

        # get user profile
        user = self._getProfileFromUser()

        # get conferenceKeysToAttend from profile.
        userConferenceKeyToAttend = user.conferenceKeysToAttend
        # to make a ndb key from websafe key you can use:
        conferenceKeys = []
        for wsck in userConferenceKeyToAttend:
            conf_key = ndb.Key(urlsafe=wsck)
            conferenceKeys.append(conf_key)

        conferences = ndb.get_multi(conferenceKeys)

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf, "")\
         for conf in conferences]
        )

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
        path='filterPlayground',
        http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        q = Conference.query()
        # simple filter usage:
        # q = q.filter(Conference.city == "Paris")
    
        # advanced filter building and usage
        field = "city"
        operator = "="
        value = "London"
        f = ndb.query.FilterNode(field, operator, value)
        q = q.filter(f)

        field = "topics"
        operator = "="
        value = "Medical Innovations"
        f = ndb.query.FilterNode(field, operator, value)
        q = q.filter(f)
    
        # TODO
        # add 2 filters:
        # 1: city equals to London
        # 2: topic equals "Medical Innovations"
    
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )

# - - - Registration - - - - - - - - - - - - - - - - - - - -

    # conference registration needs to use transactions to guarantee that a user 
    # is not fasely registered for a full conference
    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)

    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='DELETE', name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request, reg=false)

# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""

        ## step 1: make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        user_id = getUserId(user)
        # Create an instance of a Key for an id(user email) of a kind(Profile) 
        p_key = ndb.Key(Profile, user_id)
        # Get the entity(user profile) associated with the key
        profile = p_key.get()

        # If the entity does not exist, create a new entity and put in datastore
        if not profile:
            profile = Profile(
                key = p_key,
                displayName = user.nickname(), 
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
            )
            # put will store profile as a persistent entity in the Datastore
            profile.put()

        return profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
            # Update profile entity with field from profile miniform
            prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()

    # 1. change request class
    # 2. pass request to _doProfile function
    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)

# - - - Announcements - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = '%s %s' % (
                'Last chance to attend! The following conferences '
                'are nearly sold out:',
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/get',
            http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        # return an existing announcement from Memcache or an empty string.
        announcement = memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY)
        if not announcement:
            announcement = ""
        return StringMessage(data=announcement)

    @staticmethod
    def _speakerAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & getSpeakerAnnouncement().
        """
        # use -Speaker.sessionCount to specify descending order
        speakers = Speaker.query().order(-Speaker.sessionCount).fetch()

        # Get all speakers that have the most sessions. 
        featuredSpeakers = []
        for speaker in speakers:
            if featuredSpeakers == []:
                featuredSpeakers.append(speaker)
            elif speaker.sessionCount == featuredSpeakers[0].sessionCount:
                featuredSpeakers.append(speaker)

        if featuredSpeakers:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = '%s %s' % (
                'Featured Speaker presting in the most sessions'
                '',
                ', '.join(speaker.displayName for speaker in featuredSpeakers))
            memcache.set(MEMCACHE_SPEAKER_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_SPEAKER_KEY)

        return announcement

    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='speaker/announcement/get',
            http_method='GET', name='getSpeakerAnnouncement')
    def getSpeakerAnnouncement(self, request):
        """Return new speaker from memcache."""
        # return an existing announcement from Memcache or an empty string.
        announcement = memcache.get(MEMCACHE_SPEAKER_KEY)
        if not announcement:
            announcement = ""
        return StringMessage(data=announcement)


# - - - Session objects - - - - - - - - - - - - - - - - -
    @ndb.transactional(xg=True)
    def _createSessionObject(self, request, s_id):
        """Create Session object and Speaker object, returning SessionForm."""
        # Verify user is logged in
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        conf = conf_key.get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # check that the user trying to create the conference is the conference organizer
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException('Only conference organizer can create a session')
        
        # copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['websafeConferenceKey']

        # convert dates from strings to Date objects; set month based on start_date
        if data['date']:
            data['date'] = datetime.strptime(data['date'], "%Y-%m-%d").date()
        
        if data['startTime']:
            data['startTime'] = datetime.strptime(data['startTime'], "%H:%M").time()

        # Convert Enum to string
        if data['typeOfSession'] in (None, []):
            data['typeOfSession'] = 'NOT_SPECIFIED'
            setattr(request, 'typeOfSession', TypeOfSession.NOT_SPECIFIED)
        else:
            data['typeOfSession'] = str(data['typeOfSession'])

        # make Session key from ID and parent conference key
        s_key = ndb.Key(Session, s_id, parent=conf_key)
        data['key'] = s_key

        # Add Created Session to the list of Sessions the speaker is preseting in.
        speaker_key = ndb.Key(Speaker, request.speakerName)
        speaker = speaker_key.get()

        # If the speaker does not exist, create a new entity and put in datastore
        if not speaker:
            speaker = Speaker(
                key = speaker_key,
                displayName = request.speakerName, 
            )

        speaker.sessionKeysToAttend.append(s_key.urlsafe())
        speaker.featuredSpeaker = True

        # Use taskqeueu to set Feature Speaker
        taskqueue.add(params={},url='/tasks/set_speaker')

        Session(**data).put()
        speaker.put()

        return self._copySessionToForm(request)

    def _copySessionToForm(self, session):
        """Copy relevant fields from Session to SessionForm."""
        sf = SessionForm()
        for field in sf.all_fields():
            if hasattr(session, field.name):
                # convert date and startTime to string;
                if field.name == 'date' or field.name == 'startTime':
                    setattr(sf, field.name, str(getattr(session, field.name)))
                elif field.name == 'typeOfSession':
                    setattr(sf, field.name, getattr(TypeOfSession, str(getattr(session, field.name))))
                else:
                    setattr(sf, field.name, getattr(session, field.name))
            elif field.name == "websafeKey":
                setattr(sf, field.name, session.key.urlsafe())
        sf.check_initialized()
        return sf

    @endpoints.method(SESSION_CONTAINER, SessionForm, 
            path='session/{websafeConferenceKey}',
            http_method='POST', name='createSession')
    def createSession(self, request):
        """Create new session. Open to the organizer of the conference"""
        s_id = Session.allocate_ids(size=1, parent=ndb.Key(urlsafe=request.websafeConferenceKey))[0]
        return self._createSessionObject(request, s_id)

    @endpoints.method(SESSION_REQUEST, SessionForms,
            path='getConferenceSessions/{websafeConferenceKey}',
            http_method='GET',
            name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Given a conference, return all sessions."""

        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        # Query for all Session that have an ancestor Conference
        sessions = Session.query(ancestor=c_key)
    
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )

    @endpoints.method(SESSION_REQUEST, SessionForms,
            path='getConferenceSessionsByType/{websafeConferenceKey}/{sessionType}',
            http_method='GET',
            name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Given a conference, return all sessions of a specified type."""

        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        sessions = Session.query(ancestor=c_key)
        sessions = sessions.filter(Session.typeOfSession == request.sessionType)
    
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )

    @endpoints.method(SpeakerForm, SessionForms,
            path='getSessionsBySpeaker',
            http_method='GET',
            name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Given a speaker, return all sessions given by this particular speaker, across all conferences."""

        speaker_key = ndb.Key(Speaker, request.displayName)
        speaker = speaker_key.get()

        # If the speaker does not exist, create a new entity and put in datastore
        if not speaker:
            raise endpoints.NotFoundException("Speaker %s found" % request.displayName )

        # get sessionKeysToAttend from profile.
        speakerSessionsToAttend = speaker.sessionKeysToAttend
        # to make a ndb key from websafe key you can use:
        sessionKeys = []
        for wsck in speakerSessionsToAttend:
            session_key = ndb.Key(urlsafe=wsck)
            sessionKeys.append(session_key)

        # Issue a multi query for all sessionKeys contained by a Speaker
        sessions = ndb.get_multi(sessionKeys)

        # copy results to SessionForms
        return SessionForms(items=[self._copySessionToForm(session)\
         for session in sessions]
        )

    def _getSessionQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Session.query()
        inequality_filter, filters = self._formatFilters(request.filters, SESSION_FIELDS)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Session.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Session.name)

        for filtr in filters:
            if filtr["field"] == "startTime":
                testTime = datetime.strptime(filtr["value"], "%H:%M").time()
                # filtr["value"] = datetime.strptime(filtr["value"], "%H:%M").time()
                # formatted_query = ndb.query.FilterNode(Session.startTime._to_base_type, filtr["operator"], testTime)
                if filtr["operator"] == '=':
                    q = q.filter(Session.startTime == testTime)

                elif filtr["operator"] == '>=':
                    q = q.filter(Session.startTime >= testTime)

                elif filtr["operator"] == '<=':
                    q = q.filter(
                        ndb.AND(Session.startTime <= testTime),
                                (Session.startTime >= datetime.strptime("00:00", "%H:%M").time()))
                else:
                    raise endpoints.BadRequestException("Unsupported filter for time.")
            else:
                formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
                q = q.filter(formatted_query)
        return q

    @endpoints.method(SessionQueryForms, SessionForms,
            path='queryAllSessions',
            http_method='POST',
            name='queryAllSessions')
    def queryAllSessions(self, request):
        """Query for conferences."""
        sessions = self._getSessionQuery(request)
    
        return SessionForms(
            items=[self._copySessionToForm(session) \
            for session in sessions]
        )


# -------------------------------------------
    @endpoints.method(WISHLIST_REQUEST, SessionForm,
            path='addSessionToWishlist/{websafeSessionKey}',
            http_method='POST', name='addSessionToWishlist')
    def addSessionToWishlist(self, request):
        """Register user for selected conference."""
        prof = self._getProfileFromUser() # get user Profile

        # check if session exists given websafeSessionKey
        # get session; check that it exists
        wssk = request.websafeSessionKey
        session = ndb.Key(urlsafe=wssk).get()
        if not session:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % wssk)

        # check if user already registered otherwise add
        if wssk in prof.sessionsInWishlist:
            raise ConflictException(
                "Session is already in your wishlist")
        # register user, take away one seat
        prof.sessionsInWishlist.append(wssk)

        # write things back to the datastore & return
        prof.put()
        return self._copySessionToForm(session)

    @endpoints.method(message_types.VoidMessage, SessionForms,
            path='getSessionsInWishlist',
            http_method='GET', name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Register user for selected conference."""
        prof = self._getProfileFromUser() # get user Profile

        # get sessionKeysToAttend from profile.
        profSessionsInWishlist = prof.sessionsInWishlist
        # to make a ndb key from websafe key you can use:
        sessionKeys = []
        for wssk in profSessionsInWishlist:
            session_key = ndb.Key(urlsafe=wssk)
            sessionKeys.append(session_key)

        sessions = ndb.get_multi(sessionKeys)

        # return set of ConferenceForm objects per Conference
        return SessionForms(items=[self._copySessionToForm(session)\
         for session in sessions]
        )


# registers API
api = endpoints.api_server([ConferenceApi]) 
