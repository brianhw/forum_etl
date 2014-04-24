import MySQLdb
from UserDict import DictMixin
from collections import OrderedDict
from datetime import datetime
import getpass
import logging
import os
import pickle
from pymongo import MongoClient
from mongodb import MongoDB
import re
import subprocess
import sys
import warnings

from pymysql_utils.pymysql_utils import MySQLDB


class EdxForumScrubber(object):
    
    LOG_DIR = '/home/dataman/Data/EdX/NonTransformLogs'

    # Pattern for email id - strings of alphabets/numbers/dots/hyphens followed
    # by an @ or at followed by combinations of dot/. followed by the edu/com
    # also, allow for spaces
    
    emailPattern='(.*)\s+([a-zA-Z0-9\(\.\-]+)[@]([a-zA-Z0-9\.]+)(.)(edu|com)\\s*(.*)'
    #emailPattern='(.*)\\s+([a-zA-Z0-9\\.]+)\\s*(\\(f.*b.*)?(@)\\s*([a-zA-Z0-9\\.\\s;]+)\\s*(\\.)\\s*(edu|com)\\s+(.*)'
    compiledEmailPattern = re.compile(emailPattern);
    
    def __init__(self, bsonFileName, mysqlDbObj=None, forumTableName='contents', allUsersTableName='EdxPrivate.UserGrade'):
        '''
        Given a .bson file containing OpenEdX Forum entries, anonymize the entries,
        and place them into a MySQL table.  
        
        :param bsonFileName: full path the .bson table
        :type bsonFileName: String
        :param mysqlDbObj: a pymysql_utils.MySQLDB object where anonymized entries are
            to be placed. If None, a new such object is created into MySQL db 'EdxForum'
        :type mysqlDbObj: MySQLDB
        :param forumTableName: name of table into which anonymized Forum entries are to be placed
        :type forumTableName: String
        :param allUsersTable: fully qualified name of table listing all in-the-clear user names
            of users who post to the Forum. Used to redact their names from their own posts.
        :type allUsersTable: String
        '''
        
        self.bsonFileName = bsonFileName
        self.forumTableName = forumTableName
        self.allUsersTableName = allUsersTableName
        
        # If not unittest, but regular run, then mysqlDbObj is None
        if mysqlDbObj is None:
            self.mysql_passwd = self.getMySQLPasswd()
            self.mysql_dbhost ='localhost'
            self.mysql_user = getpass.getuser() # user that started this process
            #**** NEEDED? self.mysql_db = 'EdxForum'
            self.mydb = MySQLDB(user=self.mysql_user, db='EdxForum')
        else:
            self.mydb = mysqlDbObj

        self.counter=0
        
        self.userCache = {}
        self.userSet   = set()

        warnings.filterwarnings('ignore', category=MySQLdb.Warning)        
        self.prepLogging()
        self.prepDatabase()

        #******mysqldb.commit();    
        #******logging.info('commit completed!')

    def runConversion(self):
        '''
        Do the actual work. We don't call this method from __init__()
        so that unittests can create an EdxForumScrubber instance without
        doing the actual work. Instead, unittests call individual methods. 
        '''
        self.populateUserCache();

        # Load bson file into Mongodb:
        self.loadForumIntoMongoDb(self.bsonFileName)
        
        self.mongodb = MongoDB(dbName='TmpForum', collection='ForumContents')
        
        # Anonymize each forum record, and transfer to MySQL db:
        self.forumMongoToRelational(self.mongodb)

    def loadForumIntoMongoDb(self, bsonFilename):

        mongoclient = MongoClient();
        db = mongoclient[self.mongo_database_name];

        # Fix collection name
        collection = db[self.collection_name];

        # Clear out any old forum entries:
        logging.info('Preparing to delete the collection ')
        collection.remove()
        logging.info('Deleting mongo collection completed. Will now attempt a mongo restore')
        
        logging.info('Spawning subprocess to execute mongo restore')
        with open(self.logFilePath,'w') as outfile:
            ret = subprocess.call(["mongorestore", bson_filename, 
                    "-db", self.mongo_database_name, 
                    "-mongoForumRec", self.collection_name], 
                stdout=outfile, stderr=outfile)
        logging.debug('Return value from mongorestore is %s' % (ret))

    def forumMongoToRelational(self, mongodb, mysqlDbObj, mysqlTable):
        '''
        Given a pymongo collection object in which Forum posts are stored,
        and a MySQL db object and table name, anonymize each mongo record,
        and insert it into the MySQL table.
        
        :param collection: collection object obtained via a mangoclient object
        :type collection: Collection
        :param mysqlDbObj: wrapper to MySQL db. See pymysql_utils.py
        :type mysqlDbObj: MYSQLDB
        :param mysqlTable: name of table where posts are to be deposited.
            Example: 'contents'.
        :type mysqlTable: String
        '''

        #command = 'mongorestore %s -db %s -mongoForumRec %s'%(self.bson_filename,self.mongo_database_name,self.collection_name)
        #print command
    
        logging.info('Will start inserting from mongo collection to MySQL')

        for mongoForumRec in mongodb.query({}):
            mongoRecordObj = MongoRecord(mongoForumRec)

            try:
                # Check whether 'up' can be converted to a list
                list(mongoRecordObj['up'])
            except Exception as e:
                logging.info('Error in conversion' + `e`)
                mongoRecordObj['up'] ='-1'
            
            self.insert_content_record(mysqlDbObj, mysqlTable, mongoRecordObj);
        
    def prepDatabase(self):
        '''
        Declare variables and execute statements preparing the database to 
        configure options - e.g.: setting char set to utf, connection type to utf
        truncating the already existing table.
        '''
        try:
            #mysqldb=MySQLdb.connect(host=mysql_dbhost,user=mysql_user,passwd=mysql_passwd,db=mysql_db)
            #***** NEEDED? mysqldb=MySQLdb.connect(host=self.mysql_dbhost,user=self.mysql_user,passwd=self.mysql_passwd)
            
            #***** NEEDED? logging.debug("Connection to MYSql db successful %s"%(mysqldb))
            #cur = mysqldb.cursor();
            #*******Needed? mysqldb.set_character_set('utf8')
            logging.debug("Setting and assigning char set for mysqld. will truncate old values")
            self.mydb.execute('SET NAMES utf8;');
            self.mydb.execute('SET CHARACTER SET utf8;');
            self.mydb.execute('SET character_set_connection=utf8;');
            
            # Compose fully qualified table name from the db name to 
            # which self.mydb is connected, and the forum table name
            # that was established in __init__():
            fullTblName = self.mydb.dbName() + '.' + self.forumTableName
            # Clear old forum data out of the table:
            try:
                self.mydb.truncateTable(fullTblName);
                logging.debug("setting and assigning char set complete. Truncation succeeded")                
            except ValueError as e:
                # Table doesn't exist. Create it:
                self.createForumTable()
                logging.debug("setting and assigning char set complete. Created %s table." % fullTblName)
        
        except MySQLdb.Error,e:
            logging.info("MySql Error exiting %d: %s" % (e.args[0],e.args[1]))
            # print e
            sys.exit(1)
    
    def getMySQLPasswd(self):
        homeDir=os.path.expanduser('~'+getpass.getuser())
        f_name = homeDir + '/.ssh/mysql'
        try:
            with open(f_name, 'r') as f:
                password = f.readline().strip()
        except IOError:
            return ''
        return password

    def prepLogging(self):
        logFileName = 'forum_%s.log'%(datetime.now().strftime('%Y-%m-%d-%H-%M-%S'))
        self.logFilePath = os.path.join(EdxForumScrubber.LOG_DIR, logFileName)
        logging.basicConfig(filename=self.logFilePath,level=logging.DEBUG)

    def populateUserCache (self) : 
        '''
        Populate the User Cache and preload information on user id int, screen name
        and the actual name
        '''
        try:
            logging.info("Beginning to populate user cache");
            # Cache all in-the-clear user names of participants who
            # might post posts:
            for userRow in self.mydb.query('select user_int_id,name,screen_name,anon_screen_name from %s' % self.allUsersTableName):
                userCacheEntry=[]
                userCacheEntry.append(userRow[1]) # full name
                userCacheEntry.append(userRow[2]) # screen_name
                userCacheEntry.append(userRow[3]) # anon_screen_name
    
                # What is l1?
                l1=userRow[1].split()
            
                if len(l1)>0:
                    self.userSet.add(l1[0])
    
                """for word in l1:
                    if(len(word)>2 and '\\'    not in repr(word) ):
                        self.userSet.add(word)
                        if '-' in word:
                            l2=word.split('-')
                            for data in l2:
                                self.userSet.add(data)"""
                """if(len(userRow[1])>0):
                    self.userSet|=set([userRow[1]])
                    self.userSet|=set(userRow[2])"""
         
                self.userCache[int(userRow[0])]=userCacheEntry;    
            logging.info("loaded objects in usercache %d"%(len(self.userCache)))
            pickle.dump( self.userSet, open( "user.p", "wb" ) )
    
            #print self.userSet
        except MySQLdb.Error,e:
            logging.info("MySql Error while user cache exiting %d: %s" % (e.args[0],e.args[1]))
            sys.exit(1)
    
    def prune_numbers(self, body):
        '''
        Prunes phone numbers from a given string and returns the string with
        phone numbers replaced by <phoneRedac>

        :param body: forum post
        :type body: String
        :returns: body with all phone number-like substrings replaced by <phoneRedac>
        :rtype: String
        '''
        #re from stackoverflow. seems to do an awesome job at capturing all phone nos :)
        s='((?:(?:\+?1\s*(?:[.-]\s*)?)?(?:\(\s*([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9])\s*\)|([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9]))\s*(?:[.-]\s*)?)?([2-9]1[02-9]|[2-9][02-9]1|[2-9][02-9]{2})\s*(?:[.-]\s*)?([0-9]{4})(?:\s*(?:#|x\.?|ext\.?|extension)\s*(\d+))?)'
        match=re.findall(s,body)
        for phoneMatchHit in match:
            body=body.replace(phoneMatchHit[0],"<phoneRedac>")
        return body    
    
    def prune_zipcode(self, body):
        '''
        Prunes the zipcdoe from a given string and returns the string without zipcode

        :param body: forum post
        :type body: String
        '''
        s='\d{5}(?:[-\s]\d{4})?'
        match=re.findall(s,body)
        for zipcodeMatchHit in match:
            body=body.replace(zipcodeMatchHit[0],"<zipRedac>")
        return body

    def trimnames(self, body):
        '''
        Removes all person names known in the forum from the given
        post. We currently return the body unchanged, because we
        found that too many names match regular English words.  
        :param body: forum post
        :type body: String
        '''
        return body
        #Trims all firstnames and last names from the body of the post.
        
        #print 'processing body %s' %(body)
        #print 'en %s' %(len(self.userSet))
        s3=set(body.split())
        s4=s3&self.userSet
        #print 's4 is %s' %(s4)
        
        for s in s4:
            if len(s)>1 and s[0].isupper():
                body = re.sub(r"\b%s\b" % s , "NAME_REMOVED", body)
    
                #body=body.replace(s,"NAME_REMOVED")
        
     
        return body


    def insert_content_record(self, mysqlDbObj, mysqlTableName, mongoRecordObj):
        '''
        Given all fields of one forum post record, anonymize the post,
        and insert the result into EdxForum.contents.
        
        :param mysqlDbObj: MySQLDB instance into which to place transformed forum posts (see pymysql_utils)
        :type mysqlDbObj: MySQLDB
        :param mysqlTableName: Name of table into which record is to be inserted. Ex: 'contents'
        :type mysqlTalbeName: String
        :param mongoRecordObj: a Python object that contains the Forum record fields we export. 
            These instances behave like dicts.
        :type _type: MongoRecord
        '''
        #print len(self.userCache);
        #line='\t'.join(data);
        #f.write(line+'\n');    
        self.counter += 1;
        
        body = mongoRecordObj['body'].encode('utf-8').strip();
    
        body = self.prune_numbers(body) 
        body = self.prune_zipcode(body)
    
        if EdxForumScrubber.compiledEmailPattern.match(body) is not None :
            #print 'BODY before EMAIL STRIPING %s \n'%(body);
            match = re.findall(EdxForumScrubber.emailPattern,body);
            new_body = " ";
            for emailMatchHit in match:
                new_body+=(emailMatchHit[0]+" <emailRedac> " + emailMatchHit[-1]);
            #print 'NEW BODY AFTER EMAIL STRIPING %s \n'%(new_body);
            body = new_body;
        
        # Redact poster's name from the post:
        user_info = self.userCache.get(int(mongoRecordObj['user_int_id']),['xxxx','xxxx']);
        name = user_info[0];
        screen_name = user_info[1];
    
        if(len(user_info) == 3):
            anon_s = user_info[2]
            anon_s = " "
        else:
            anon_s = " "
    
        #flag=0;
        #body=body.encode('utf-8').strip();
        try:
            for s in name.split() :
                if len(s) >= 3:
                    #flag=1;
                    if s.lower() in body.lower():
                        #print 'NEW BODY found before NAME STRIPING %s \n'%(body);
                        pat=re.compile(s,re.IGNORECASE);
                        anon_s=''
                        body=pat.sub("<nameRedac_"+anon_s+">",body);
                        #body.replace(s,"<NAME REMOVED>");
        except Exception as e:
            logging.info("Error while replacing name in forum post: %s. Body:\n    %s" % (`e`, body))
            #print 'blah %s -- %s'%(body,s)
    
        screenNamePattern = re.compile(screen_name,re.IGNORECASE);
        body = screenNamePattern.sub("<nameRedac_"+anon_s+">",body);    
     
        body = self.trimnames(body)
     
        #    if ('REMOVED' in body or 'CLIPPED' in body) :
        #         print 'NEW COMBINED BODY AFTER NAME STRIPING %s \n'%(body);
        try:
        #        cur.execute("insert into EdxForum.contents(type,anonymous,anonymous_to_peers,at_position_list,user_int_id,body,course_display_name,created_at,votes,count,down_count,up_count,up,down) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",(_type,anonymous,anonymous_to_peers,at_position_list,author_id,body,course_id,created_at,votes,count,down_count,up_count,up,down));
        #        print "BOOHOO %s %s %s %s %s %s %s blah %s"%(_type,anonymous,anonymous_to_peers,at_position_list,author_id,course_id,created_at,str(body))
        #        print "insert into EdxForum.contents(type,anonymous,anonymous_to_peers,at_position_list,user_int_id,body,course_display_name,created_at,votes,count,down_count,up_count,up,down) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"%(_type,anonymous,anonymous_to_peers,at_position_list,author_id,body,course_id,created_at,votes,count,down_count,up_count,up,down)
        #        print "insert into EdxForum.contents(type,anonymous,anonymous_to_peers,at_position_list,user_int_id,body,course_display_name,created_at,votes,count,down_count,up_count,up,down) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"%tup
        #         body='d'
        #         print 'inserting body %s'%(body)
        #         mydb.executeParameterized('insert into EdxForum.contents(anonymous,body) values (%s,%s)',(anonymous,body))
            fullTblName = mysqlDbObj.dbName() + '.' + mysqlTableName
            mongoRecordObj['body'] = body
            self.mydb.insert(fullTblName, mongoRecordObj)            
        #    mysqlDbObj.executeParameterized("insert into %s(type,anonymous,anonymous_to_peers,at_position_list,user_int_id,body,course_display_name,created_at,votes,count,down_count,up_count,up,down) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        #                                   (fullTblName,_type,anonymous,anonymous_to_peers,at_position_list,author_id,body,course_id,created_at,votes,count,down_count,up_count,up,down));
            
        except MySQLdb.Error as e:
            logging.info("MySql Error exiting while inserting record %d: %s authorid %s created_at %s " % \
                         (e.args[0],e.args[1],mongoRecordObj.getUserNameClear(), mongoRecordObj['created_at']))
            logging.info(" values(%s)" % str(mongoRecordObj.items()))
            sys.exit(1)
    
        if(self.counter%100 == 0):
            logging.info('inserted record %d'%( self.counter))
            # print '_type,anonymous,anonymous_to_peers,at_position_list,author_id,body,course_id,created_at,votes
            #print '%d value \n'%(self.counter);

    def createForumTable(self):
        '''
        Create an empty EdxForum.contents table. Requires
        CREATE privileges;
        '''
        createCmd = "CREATE TABLE `contents` ( \
                  `type` varchar(20) NOT NULL, \
                  `anonymous` varchar(10) NOT NULL, \
                  `anonymous_to_peers` varchar(10) NOT NULL, \
                  `at_position_list` varchar(200) NOT NULL, \
                  `user_int_id` int(11) NOT NULL, \
                  `body` varchar(2500) NOT NULL, \
                  `course_display_name` varchar(100) NOT NULL, \
                  `created_at` datetime NOT NULL, \
                  `votes` varchar(200) NOT NULL, \
                  `count` int(11) NOT NULL, \
                  `down_count` int(11) NOT NULL, \
                  `up_count` int(11) NOT NULL, \
                  `up` varchar(200) DEFAULT NULL, \
                  `down` varchar(200) DEFAULT NULL, \
                  `comment_thread_id` varchar(255) DEFAULT NULL, \
                  `parent_id` varchar(255) DEFAULT NULL, \
                  `parent_ids` varchar(255) DEFAULT NULL, \
                  `sk` varchar(255) DEFAULT NULL \
                ) ENGINE=MYISAM DEFAULT CHARSET=latin1"
                
        self.mydb.execute(createCmd)

class MongoRecord(DictMixin):
    
    def __init__(self, rawMongoStruct):
        self.nameValueDict = self.makeDict(rawMongoStruct)
        self.user_name_clear = rawMongoStruct.get('author_username')

    def getUserNameClear(self):
        return self.user_name_clear

    def makeDict(self, mongoRecordStruct):

        # Create a dict of the raw Mongo name/value pairs, converting
        # types where needed. Recall: dict.get(key,[default]) returns
        # None if no default is provided. Need an ordered dict of these
        # column names, so that they match up with column values elsewhere:
        mongoRecordDict = OrderedDict(
                         {
                           'type' : str(mongoRecordStruct.get('_type')),
                		   'anonymous' : str(mongoRecordStruct.get('anonymous')),
                		   'anonymous_to_peers' : str(mongoRecordStruct.get('anonymous_to_peers')),
                		   'at_position_list' : str(mongoRecordStruct.get('at_position_list')),
                		   'user_int_id' : mongoRecordStruct.get('author_id'), # numeric id
                		   'body' : mongoRecordStruct.get('body'),
                		   'course_display_name' : str(mongoRecordStruct.get('course_id')),
                		   'created_at' : mongoRecordStruct.get('created_at'),
                		   'votes' : str(mongoRecordStruct.get('votes')),
                           }) 
        
        votesObject= mongoRecordStruct.get('votes')
        if votesObject is not None:
            mongoRecordDict['count'] = votesObject.get('count')
            mongoRecordDict['down_count'] = votesObject.get('down_count')
            mongoRecordDict['up_count'] = votesObject.get('up_count')
            mongoRecordDict['up'] = str(votesObject.get('up'))
            if mongoRecordDict['up'] is not None:
                mongoRecordDict['up'] = mongoRecordDict['up'].replace("u","")
            mongoRecordDict['down'] = str(votesObject.get('down'))
            if mongoRecordDict['down'] is not None:
                mongoRecordDict['down'] = mongoRecordDict['down'].replace("u","")
        
        mongoRecordDict['sk'] = mongoRecordStruct.get('sk')
        mongoRecordDict['comment_thread_id'] = mongoRecordStruct.get('comment_thread_id')
        mongoRecordDict['parent_id'] = mongoRecordStruct.get('parent_id')
        mongoRecordDict['parent_ids'] = mongoRecordStruct.get('parent_ids')
        
        return mongoRecordDict

    def __getitem__(self, key):
        return self.nameValueDict[key]
    
    def __setitem__(self, key, value):
        self.nameValueDict[key] = value
    
    def __delitem__(self, key):
        del self.nameValueDict[key]
    
    def keys(self):
        return self.nameValueDict.keys()
        
#        ObjectId("519461545924670200000005")
#    ],
"""collectionObject=collection.find_one();

def generateInsertQuery(collectionobject)
    insertStatement='insert into EdxForum.contents(%s) values(%s)';
    cols=', '.join(collectionobject);
    vals=', '.join('?'*len(collectionobject));
    query=insertStatement%(cols,vals)
    db=MySQLdb.connect(host="localhost",user='root',passwd='root',db='test')
    cur=db.cursor();
    cur.execute();
"""

if __name__ == '__main__':
    if(len(sys.argv)!=2):
        print 'Usage: %s <forum_bson_filename>' % sys.argv[0]
        sys.exit(0)
    bson_filename=sys.argv[1]
    #print bson_filename
    extractor = EdxForumScrubber(bson_filename)
    extractor.runConversion()
    
    