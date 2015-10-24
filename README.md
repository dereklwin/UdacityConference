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
Speaker Models are designed to contain a repeating Property type for the Sessions a Speaker will be presenting in. Since we do not request for any other unique information for a Speaker, we require the Speaker Name to be unique so that it can be used as the Key. To determine who the Feature Speaker is, we use the Computed Property Type to count the number of Sessions a Speaker will be preseting in. 

Additional Queries
------------------
two additional Queries:
queryAllSessions API was added to filter All sessions by time and session type.

How would you handle a query for all non-workshop sessions before 7 pm? What is the problem for implementing this query? What ways to solve it did you think of?
-Using the API queryAllSession, a user can issue multiple filters to a query. The API will first order all Sessions by time. It will filter out all sessions that are not of type workshop. Then the API will format the user input time to a DateTime object and filter all times that are before 19:00 and after 00:00. Filtering times after 00:00 removes all Session Entities that do not have a a startTime Property filled.