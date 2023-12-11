"""
ClickHouse data lake implementation.
"""
import os
import uuid
from datetime import datetime

import clickhouse_connect


class XAPILakeClickhouse:
    """
    Lake implementation for ClickHouse.
    """

    client = None

    def __init__(
        self,
        db_host="localhost",
        db_port=18123,
        db_username="default",
        db_password=None,
        db_name=None,
        db_event_sink_name=None,
        s3_key=None,
        s3_secret=None,
    ):
        self.host = db_host
        self.port = db_port
        self.username = db_username
        self.database = db_name
        self.event_sink_database = db_event_sink_name
        self.db_password = db_password
        self.s3_key = s3_key
        self.s3_secret = s3_secret

        self.event_raw_table_name = "xapi_events_all"
        self.event_table_name = "xapi_events_all_parsed"
        self.event_table_name_mv = "xapi_events_all_parsed_mv"
        self.get_org_function_name = "get_org_from_course_url"
        self.set_client()

    def set_client(self):
        """
        Set up the ClickHouse client and connect.
        """
        client_options = {
            "date_time_input_format": "best_effort",  # Allows RFC dates
            "allow_experimental_object_type": 1,  # Allows JSON data type
        }

        # For some reason get_client isn't automatically setting secure based on the port
        # so we have to do it ourselves. This is obviously limiting, but should be 90% correct
        # and keeps us from adding yet another command line option.
        secure = str(self.port).endswith("443") or str(self.port).endswith("440")

        self.client = clickhouse_connect.get_client(
            host=self.host,
            username=self.username,
            password=self.db_password,
            port=self.port,
            database=self.database,
            settings=client_options,
            secure=secure,
        )

    def print_db_time(self):
        """
        Print the current time according to the db.
        """
        res = self.client.query("SELECT timezone(), now()")
        # Always flush our output on these so we can follow the logs.
        print(res.result_set, flush=True)

    def print_row_counts(self):
        """
        Print the current row count.
        """
        print("Hard table row count:")
        res = self.client.query(f"SELECT count(*) FROM {self.event_table_name}")
        print(res.result_set)

    def batch_insert(self, events):
        """
        Insert a batch of events to ClickHouse.
        """
        out_data = []
        for v in events:
            try:
                out = f"('{v['event_id']}', '{v['emission_time']}', '{v['event']}', '{v['event']}')"
                out_data.append(out)
            except Exception:
                print(v)
                raise
        vals = ",".join(out_data)
        sql = f"""
                INSERT INTO {self.event_raw_table_name} (
                    event_id,
                    emission_time,
                    event,
                    event_str
                )
                VALUES {vals}
            """

        self._insert_sql_with_retry(sql)

    def insert_event_sink_course_data(self, courses):
        """
        Insert the course overview data to ClickHouse.

        This allows us to test join performance to get course and block names.
        """
        out_data = []
        for course in courses:
            c = course.serialize_course_data_for_event_sink()
            dump_id = str(uuid.uuid4())
            dump_time = datetime.utcnow()
            try:
                out = f"""(
                    '{c['org']}',
                    '{c['course_key']}',
                    '{c['display_name']}',
                    '{c['course_start']}',
                    '{c['course_end']}',
                    '{c['enrollment_start']}',
                    '{c['enrollment_end']}',
                    '{c['self_paced']}',
                    '{c['course_data_json']}',
                    '{c['created']}',
                    '{c['modified']}',
                    '{dump_id}',
                    '{dump_time}'
                )"""
                out_data.append(out)
            except Exception:
                print(c)
                raise
        vals = ",".join(out_data)
        sql = f"""
                INSERT INTO {self.event_sink_database}.course_overviews
                VALUES {vals}
            """

        self._insert_sql_with_retry(sql)

    def insert_event_sink_block_data(self, courses):
        """
        Insert the block data to ClickHouse.

        This allows us to test join performance to get course and block names.
        """
        for course in courses:
            out_data = []
            blocks = course.serialize_block_data_for_event_sink()
            dump_id = str(uuid.uuid4())
            dump_time = datetime.utcnow()
            for b in blocks:
                try:
                    out = f"""(
                        '{b['org']}',
                        '{b['course_key']}',
                        '{b['location']}',
                        '{b['display_name']}',
                        '{b['xblock_data_json']}',
                        '{b['order']}',
                        '{b['edited_on']}',
                        '{dump_id}',
                        '{dump_time}'
                    )"""
                    out_data.append(out)
                except Exception:
                    print(b)
                    raise

            vals = ",".join(out_data)
            sql = f"""
                    INSERT INTO {self.event_sink_database}.course_blocks
                    VALUES {vals}
                """

            self._insert_sql_with_retry(sql)

    def insert_event_sink_actor_data(self, actors):
        """
        Insert the user_profile and external_id data to ClickHouse.

        This allows us to test PII reports.
        """
        out_external_id = []
        out_profile = []
        for actor in actors:
            dump_id = str(uuid.uuid4())
            dump_time = datetime.utcnow()
            try:
                id_row = f"""(
                    '{actor.id}',
                    'xapi',
                    '{actor.username}',
                    '{actor.user_id}',
                    '{dump_id}',
                    '{dump_time}'
                )"""
                out_external_id.append(id_row)

                # This first column is usually the MySQL row pk, we just
                # user this for now to have a unique id.
                profile_row = f"""(
                    '{actor.user_id}',
                    '{actor.user_id}',
                    '{actor.name}',
                    '{actor.meta}',
                    '{actor.courseware}',
                    '{actor.language}',
                    '{actor.location}',
                    '{actor.year_of_birth}',
                    '{actor.gender}',
                    '{actor.level_of_education}',
                    '{actor.mailing_address}',
                    '{actor.city}',
                    '{actor.country}',
                    '{actor.state}',
                    '{actor.goals}',
                    '{actor.bio}',
                    '{actor.profile_image_uploaded_at}',
                    '{actor.phone_number}',
                    '{dump_id}',
                    '{dump_time}'
                )"""

                out_profile.append(profile_row)
            except Exception:
                print(actor)
                raise

        # Now do the actual inserts...
        vals = ",".join(out_external_id)
        sql = f"""
                INSERT INTO {self.event_sink_database}.external_id
                VALUES {vals}
            """
        self._insert_sql_with_retry(sql)

        vals = ",".join(out_profile)
        sql = f"""
                INSERT INTO {self.event_sink_database}.user_profile
                VALUES {vals}
            """
        self._insert_sql_with_retry(sql)

    def _insert_sql_with_retry(self, sql):
        """
        Wrap insert commands with a single retry.
        """
        # Sometimes the connection randomly dies, this gives us a second shot in that case
        try:
            self.client.command(sql)
        except clickhouse_connect.driver.exceptions.OperationalError:
            print("ClickHouse OperationalError, trying to reconnect.")
            self.set_client()
            print("Retrying insert...")
            self.client.command(sql)
        except clickhouse_connect.driver.exceptions.DatabaseError:
            print("ClickHouse DatabaseError:")
            print(sql)
            raise

    def load_from_s3(self, s3_location):
        """
        Load generated csv.gz files from S3.

        This does a bulk file insert directly from S3 to ClickHouse, so files
        never get downloaded directly to the local process.
        """
        loads = (
            (f"{self.event_sink_database}.course_overviews", os.path.join(s3_location, "courses.csv.gz")),
            (f"{self.event_sink_database}.course_blocks", os.path.join(s3_location, "blocks.csv.gz")),
            (f"{self.event_sink_database}.external_id", os.path.join(s3_location, "external_ids.csv.gz")),
            (f"{self.event_sink_database}.user_profile", os.path.join(s3_location, "user_profiles.csv.gz")),
            (f"{self.database}.{self.event_raw_table_name}", os.path.join(s3_location, "xapi.csv.gz"))
        )

        for table_name, file_path in loads:
            print(f"Inserting into {table_name}")

            sql = f"""
            INSERT INTO {table_name}
               SELECT *
               FROM s3('{file_path}', '{self.s3_key}', '{self.s3_secret}', 'CSV');
            """

            self.client.command(sql)
            self.print_db_time()

    def finalize(self):
        """
        Nothing to finalize here.
        """

    def _run_query_and_print(self, query_name, query):
        """
        Execute a ClickHouse query and print the elapsed client time.
        """
        print(query_name)
        start_time = datetime.utcnow()
        result = self.client.query(query)
        end_time = datetime.utcnow()
        print(result.summary)
        print(result.result_set[:10])
        print("Completed in: " + str((end_time - start_time).total_seconds()))
        print("=================================")

    def do_queries(self, event_generator):
        """
        Query data from the table and document how long the query runs (while the insert script is running).
        """
        # Get our randomly selected targets for this run
        course = event_generator.get_course()
        course_url = course.course_url
        org = event_generator.get_org()
        actor = course.get_enrolled_actor().actor.id

        self._run_query_and_print(
            "Count of enrollment events for course {course_url}",
            f"""
                select count(*)
                from {self.event_table_name}
                where course_id = '{course_url}'
                and verb_id = 'http://adlnet.gov/expapi/verbs/registered'
            """,
        )

        self._run_query_and_print(
            "Count of total enrollment events for org {org}",
            f"""
                select count(*)
                from {self.event_table_name}
                where org = '{org}'
                and verb_id = 'http://adlnet.gov/expapi/verbs/registered'
            """,
        )

        self._run_query_and_print(
            "Count of enrollments for this actpr",
            f"""
                select count(*)
                from {self.event_table_name}
                where actor_id = '{actor}'
                and verb_id = 'http://adlnet.gov/expapi/verbs/registered'
            """,
        )

        self._run_query_and_print(
            "Count of enrollments for this course - count of unenrollments, last 30 days",
            f"""
                select a.cnt, b.cnt, a.cnt - b.cnt as total_registrations
                from (
                select count(*) cnt
                from {self.event_table_name}
                where course_id = '{course_url}'
                and verb_id = 'http://adlnet.gov/expapi/verbs/registered'
                and emission_time between date_sub(DAY, 30, now('UTC')) and now('UTC')) as a,
                (select count(*) cnt
                from {self.event_table_name}
                where course_id = '{course_url}'
                and verb_id = 'http://id.tincanapi.com/verb/unregistered'
                and emission_time between date_sub(DAY, 30, now('UTC')) and now('UTC')) as b
            """,
        )

        # Number of enrollments for this course - number of unenrollments, all time
        self._run_query_and_print(
            "Count of enrollments for this course - count of unenrollments, all time",
            f"""
                select a.cnt, b.cnt, a.cnt - b.cnt as total_registrations
                from (
                select count(*) cnt
                from {self.event_table_name}
                where course_id = '{course_url}'
                and verb_id = 'http://adlnet.gov/expapi/verbs/registered'
                ) as a,
                (select count(*) cnt
                from {self.event_table_name}
                where course_id = '{course.course_id}'
                and verb_id = 'http://id.tincanapi.com/verb/unregistered'
                ) as b
            """,
        )

        self._run_query_and_print(
            "Count of enrollments for all courses - count of unenrollments, last 5 minutes",
            f"""
                select a.cnt, b.cnt, a.cnt - b.cnt as total_registrations
                from (
                select count(*) cnt
                from {self.event_table_name}
                where verb_id = 'http://adlnet.gov/expapi/verbs/registered'
                and emission_time between date_sub(MINUTE, 5, now('UTC')) and now('UTC')) as a,
                (select count(*) cnt
                from {self.event_table_name}
                where verb_id = 'http://id.tincanapi.com/verb/unregistered'
                and emission_time between date_sub(MINUTE, 5, now('UTC')) and now('UTC')) as b
            """,
        )

    def do_distributions(self):
        """
        Execute and print the timing of distribution queries to enable comparisons across runs.
        """
        self._run_query_and_print(
            "Count of courses",
            f"""
               select count(distinct course_id)
               from {self.event_table_name}
           """,
        )

        self._run_query_and_print(
            "Count of actors",
            f"""
               select count(distinct actor_id)
               from {self.event_table_name}
           """,
        )

        self._run_query_and_print(
            "Count of verbs",
            f"""
               select count(*), verb_id
               from {self.event_table_name}
               group by verb_id
           """,
        )

        self._run_query_and_print(
            "Count of orgs",
            f"""
               select count(*), org
               from {self.event_table_name}
               group by org
           """,
        )

        self._run_query_and_print(
            "Avg, min, max actors per course",
            f"""
                select avg(a.num_actors) as avg_actors,
                        min(a.num_actors) as min_actors,
                        max(a.num_actors) max_actors
                from (
                    select count(distinct actor_id) as num_actors
                    from {self.event_table_name}
                    group by course_id
                ) a
            """,
        )

        self._run_query_and_print(
            "Avg, min, max problems per course",
            f"""
               select avg(a.num_problems) as avg_problems, min(a.num_problems) as min_problems,
                    max(a.num_problems) max_problems
                from (
                    select count(distinct object_id) as num_problems
                    from {self.event_table_name}
                    where JSON_VALUE(event_str, '$.object.definition.type') =
                    'http://adlnet.gov/expapi/activities/cmi.interaction'
                    group by course_id
                ) a
           """,
        )

        self._run_query_and_print(
            "Avg, min, max videos per course",
            f"""
               select avg(a.num_videos) as avg_videos, min(a.num_videos) as min_videos,
               max(a.num_videos) max_videos
               from (
                   select count(distinct object_id) as num_videos
                   from {self.event_table_name}
                   where JSON_VALUE(event_str, '$.object.definition.type') =
                    'https://w3id.org/xapi/video/activity-type/video'
                   group by object_id
               ) a
           """,
        )

        self._run_query_and_print(
            "Random event by id",
            f"""
                select *
                from {self.event_table_name}
                where event_id = (
                    select event_id
                    from {self.event_table_name}
                    limit 1
                )
            """,
        )
