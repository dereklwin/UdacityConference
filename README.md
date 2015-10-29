Conference Central

Conference Central is a cloud-based API server that utilizes Google Cloud Platform to build scalable applications. 

To Access the API's
--------------------
Visit:
https://uconference-1096.appspot.com/_ah/api/explorer

or:
Setup Instructions:
1. Update the value of `application` in `app.yaml` to the app ID you
   have registered in the App Engine admin console and would like to use to host
   your instance of this sample.
2. Update the values at the top of `settings.py` to
   reflect the respective client IDs you have registered in the.
3. Update the value of CLIENT_ID in `static/js/app.js` to the Web client ID
4. Use Google App Engine Launder to deply and test the application. 


Model Descriptsion
------------------
Session Model:
Session NDB model contains basic information needed to describe a Session in a Conference. Name and 
speaker name are the minimum required filed to create a Session. date and start time use DateProperty and TimeProperty to make it easier to store filter queires on those types properly. 


Speaker Model:
Speakers are implimented through the Pofile model. The Profile model already contains most of the information needed for a speaker. It is cleaner to add a key property to the Profile model and store the session keys that a speaker will present in. The profile model uses email address as the unique key which addresses speakers with the same name. This enhancement of Profile kind does not modify the behavior of the Profile APIs. 

Additional Queries
------------------
two additional Queries:
queryAllSessions API was added to filter All sessions by time and session type.

How would you handle a query for all non-workshop sessions before 7 pm? What is the problem for implementing this query? What ways to solve it did you think of?

The problem with using a (!=) to query for all non-workshop sessions is that it results in inequality filters being applied to more than one property. Datastore handles the not-equal operator by joining a less-than(<) and greater than(>) query. So if we query for both non-workshop sessions and sessions before 7pm, it results in inequality filters being applied to two separate properties (typeOfSession and startTime). 

One way to work around this issue is to do the use python to do the second inequality filter. For example, we will issue the inequality filter on every conference after 7pm. In python, we will filter for all sessions not equal to a certain type before copying it to SessionForm.


You might want to make sure that the key provided by the User actually points to a Session object and not some other ndb Entity. As it is this endpoint will happily accept a key pointing to anything - which could cause problems down the road
how to check a websafekey is of type 