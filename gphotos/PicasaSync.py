#!/usr/bin/python
# coding: utf8
import os.path
import shutil
import urllib
from datetime import timedelta, datetime

import gdata.gauth
import gdata.photos.service

import Utils
from AlbumMedia import AlbumMedia
from LocalData import LocalData
from PicasaMedia import PicasaMedia
from gphotos.DatabaseMedia import DatabaseMedia, MediaType


class PicasaSync(object):
    """A Class for managing the indexing and download of media via the
    picasa web API.
    """
    # noinspection SpellCheckingInspection
    PHOTOS_QUERY = '/data/feed/api/user/default/albumid/{0}'
    BLOCK_SIZE = 1000
    ALBUM_MAX = 10000  # picasa web api gets 500 response after 10000 files
    HIDDEN_ALBUMS = [u'Profile Photos']
    ALL_FILES_ALBUMS = [u'Auto Backup']

    def __init__(self, credentials, root_folder, db):
        """
        :param (OAutCredentials) credentials:
        :param (str) root_folder:
        :param (LocalData) db:
        """
        self._root_folder = root_folder
        self._db = db
        self._gdata_client = None
        self._credentials = credentials
        self._auth2token = gdata.gauth.OAuth2TokenFromCredentials(credentials)

        gd_client = gdata.photos.service.PhotosService()
        orig_request = gd_client.http_client.request
        gd_client = self._auth2token.authorize(gd_client)
        gd_client = Utils.patch_http_client(self._auth2token, gd_client,
                                            orig_request)
        gd_client.additional_headers = {
            'Authorization': 'Bearer %s' % self._credentials.access_token}
        self._gdata_client = gd_client

        # public members to be set after init
        self.startDate = None
        self.endDate = None
        self.album_name = None
        self.quiet = False
        self.includeVideo = False

    FEED_URI = '/data/feed/api/user/default?kind={0}'

    def index_picasa_media(self):
        print('\nIndexing Picasa Files ...')
        uri = self.FEED_URI.format('photo')
        start_entry = 1
        limit = PicasaSync.BLOCK_SIZE
        while True:
            photos = Utils.retry(10, self._gdata_client.GetFeed, uri,
                                 limit=limit, start_index=start_entry)
            count = len(photos.entry)
            start_entry += count
            print('indexing {0} photos ...'.format(count))

            for photo in photos.entry:
                media = PicasaMedia(None, self._root_folder, photo)
                results = self._db.get_file_by_id(media.id)
                if not results:
                    media.save_to_db(self._db)

            if count < 48:
                break

    def download_picasa_media(self):
        print('\nDownloading Picasa Only Files ...')
        # noinspection PyTypeChecker
        for media in DatabaseMedia.get_media_by_search(
                self._root_folder, self._db, media_type=MediaType.PICASA,
                start_date=self.startDate, end_date=self.endDate):
            if os.path.exists(media.local_full_path):
                continue

            if not self.quiet:
                print("  Downloading %s ..." % media.local_full_path)
            tmp_path = os.path.join(media.local_folder, '.gphoto.tmp')

            if not os.path.isdir(media.local_folder):
                os.makedirs(media.local_folder)

            res = Utils.retry(5, urllib.urlretrieve, media.url, tmp_path)
            if res:
                os.rename(tmp_path, media.local_full_path)
                # set the access date to create date since there is nowhere
                # else to put it on linux (and is useful for debugging)
                os.utime(media.local_full_path,
                         (Utils.to_timestamp(media.modify_date),
                          Utils.to_timestamp(media.create_date)))
            else:
                print("  failed to download %s" % media.local_path)

    def create_album_content_links(self):
        print("\nCreating album folder links to media ...")
        # the simplest way to handle moves or deletes is to clear out all links
        # first, these are quickly recreated anyway
        links_root = os.path.join(self._root_folder, 'albums')
        if os.path.exists(links_root):
            single_backup = links_root + '.previous'
            if os.path.exists(single_backup):
                shutil.rmtree(single_backup)
            os.rename(links_root, single_backup)

        for (path, file_name, album_name, end_date) in \
                self._db.get_album_files():
            if album_name in self.ALL_FILES_ALBUMS:
                continue

            full_file_name = os.path.join(path, file_name)

            year = Utils.safe_str_time(Utils.string_to_date(end_date), '%Y')
            month = Utils.safe_str_time(Utils.string_to_date(end_date), '%m%d')

            rel_path = u"{0} {1}".format(month, album_name)
            link_folder = unicode(os.path.join(links_root, year, rel_path))
            link_file = unicode(os.path.join(link_folder, file_name))
            if not os.path.islink(link_file):
                if not os.path.isdir(link_folder):
                    os.makedirs(link_folder)
                if os.path.exists(link_file):
                    # todo need duplicate handling here
                    print(u"Name clash on link {}".format(link_file))
                else:
                    os.symlink(full_file_name, link_file)

        print("album links done.\n")

    # todo this will currently do nothing unless using --flush-db
    def check_for_removed(self):
        # note for partial scans using date filters this is still OK because
        # for a file to exist it must have been indexed in a previous scan
        print('\nFinding deleted media ...')
        top_dir = os.path.join(self._root_folder, PicasaMedia.MEDIA_FOLDER)
        for (dir_name, _, file_names) in os.walk(top_dir):
            for file_name in file_names:
                file_id = self._db.get_file_by_path(dir_name, file_name)
                if not file_id:
                    name = os.path.join(dir_name, file_name)
                    os.remove(name)
                    print(u"{} deleted".format(name))

    def match_drive_photo(self, media):
        sync_row = self._db.find_file_ids_dates(size=media.size)
        if sync_row and len(sync_row) == 1:
            return sync_row

        sync_row = self._db.find_file_ids_dates(filename=media.filename)
        if sync_row and len(sync_row) == 1:
            return sync_row

        if sync_row and len(sync_row) > 1:
            sync_row = self._db.find_file_ids_dates(filename=media.filename,
                                                    size=media.size)
            # multiple matches here represent the same image (almost certainly!)
            if sync_row:
                return sync_row[0:1]

        sync_row = self._match_by_date(media)
        if sync_row:
            print(u'MATCH BY DATE on {} {}'.format(media.filename,
                                                   media.modify_date))
            return sync_row

        # not found anything or found >1 result
        return sync_row

    def _match_by_date(self, media):
        """
        search with date need to check for timezone slips due to camera not
        set to correct timezone and missing or corrupted exif_date,
        in which case revert to create date
        ABOVE DONE
        todo verify that the above is required in my photos collection
        todo todo temp removed date loop for performance on windows test
        todo confirmed windows scan is much faster - leaving for now

        :param (PicasaMedia) media: media item to find a match on
        :return ([(str, str)]): list of (file_id, date)
        """
        for use_create_date in [False]:
            sync_row = self._db.find_file_ids_dates(filename=media.filename,
                                                    exif_date=media.modify_date,
                                                    use_create=use_create_date)
            if sync_row:
                return sync_row
            for hour_offset in range(-1, 1):
                date_to_check = media.modify_date + timedelta(hours=hour_offset)
                sync_row = self._db.find_file_ids_dates(
                    filename=media.filename,
                    exif_date=date_to_check,
                    use_create=use_create_date)
            if sync_row:
                return sync_row

        return None

    def index_album_media(self, limit=None):
        """
        query picasa web interface for a list of all albums and index their
        contents into the db
        :param (int) limit: only scan this number of albums (for testing)
        """
        print('\nIndexing Albums ...')
        albums = Utils.retry(10, self._gdata_client.GetUserFeed, limit=limit)
        print('Album count %d\n' % len(albums.entry))

        helper = IndexAlbumHelper(self)

        for p_album in albums.entry:
            album = AlbumMedia(p_album)
            log = u'  Album: {}, photos: {}, updated: {}, published: {}'.format(
                album.filename, album.size, album.modify_date,
                album.create_date)
            helper.setup_next_album(album)
            if helper.skip_this_album():
                continue
            if not self.quiet:
                print(log)

            # noinspection SpellCheckingInspection
            q = p_album.GetPhotosUri() + "&imgmax=d"

            # Each iteration below processes a BLOCK_SIZE list of photos
            start_entry = 1
            limit = PicasaSync.BLOCK_SIZE
            while True:
                photos = Utils.retry(10, self._gdata_client.GetFeed, q,
                                     limit=limit, start_index=start_entry)
                helper.index_photos(photos)

                start_entry += PicasaSync.BLOCK_SIZE
                if start_entry + PicasaSync.BLOCK_SIZE > PicasaSync.ALBUM_MAX:
                    limit = PicasaSync.ALBUM_MAX - start_entry
                    print ("LIMITING ALBUM TO 10000 entries")
                if limit == 0 or len(photos.entry) < limit:
                    break

            helper.complete_album()
        helper.complete_scan()

        print('\nTotal Album Photos in Drive %d, Picasa %d, multiples %d' % (
            helper.total_photos, helper.picasa_photos,
            helper.multiple_match_count))  # Making this a 'friend' class of
        # PicasaSync by ignoring protected access


# noinspection PyProtectedMember
class IndexAlbumHelper:
    """
    This class is simply here to break up the logic of indexing albums into
    readable sized functions. I allow it to access private members of the
    parent class PicasaSync.
    """

    def __init__(self, picasa_sync):
        # Initialize members global to the whole scan
        self.p = picasa_sync
        self.total_photos = 0
        self.picasa_photos = 0
        self.drive_photos = 0
        self.multiple_match_count = 0
        (_, self.latest_download) = self.p._db.get_scan_dates()
        if not self.latest_download:
            self.latest_download = Utils.minimum_date()

        # declare members that are per album within the scan
        self.album = None
        self.album_end_photo = None
        self.album_start_photo = None
        self.sync_date = None

    def setup_next_album(self, album):
        """
        Initialize members that are per album within the scan.

        :param (AlbumMedia) album:
        """
        self.album = album
        self.album_end_photo = Utils.minimum_date()
        self.album_start_photo = album.modify_date
        self.sync_date = self.p._db.get_album(self.album.id).SyncDate
        if self.sync_date:
            self.sync_date = self.sync_date
        else:
            self.sync_date = Utils.minimum_date()

        # start up the album processing
        self.total_photos += int(album.size)

    def skip_this_album(self):
        if (self.p.album_name and self.p.album_name !=
            self.album.filename) or self.album.filename in \
                PicasaSync.HIDDEN_ALBUMS:
            return True
        if self.p.endDate:
            if Utils.string_to_date(self.p.endDate) < self.album.modify_date:
                return True
        if self.p.startDate:
            if Utils.string_to_date(self.p.startDate) > self.album.modify_date:
                return True
        # handle incremental backup but allow startDate to override
        if not self.p.startDate:
            if self.album.modify_date < self.sync_date and not \
                            self.album.filename in PicasaSync.ALL_FILES_ALBUMS:
                # Always scan ALL_FILES for updates to last 10000 picasa photos
                return True
        if int(self.album.size) == 0:
            return True
        return False

    def set_album_dates(self, photo_date):
        # make the album dates cover the range of its contents
        if self.album_end_photo < photo_date:
            self.album_end_photo = photo_date
        if self.album_start_photo > photo_date:
            self.album_start_photo = photo_date

    def put_new_picasa_media(self, media):
        self.picasa_photos += 1
        new_file_key = media.save_to_db(self.p._db)
        # store link between album and drive file
        self.p._db.put_album_file(self.album.id, new_file_key)
        self.set_album_dates(media.modify_date)
        if not self.p.quiet:
            print(u"Added {} {}".format(self.picasa_photos,
                                        media.local_full_path))

    def index_photos(self, photos):
        for photo in photos.entry:
            media = PicasaMedia(None, self.p._root_folder, photo)
            if (not self.p.includeVideo) and \
                    media.mime_type.startswith('video/'):
                continue

            picasa_row = media.is_indexed(self.p._db)
            if picasa_row:
                if media.modify_date > picasa_row.ModifyDate:
                    print(u"Updated {}".format(media.local_full_path))
                    media.save_to_db(self.p._db, update=True)
                else:
                    continue

            rows = self.p.match_drive_photo(media)
            if rows and len(rows) == 1:
                row = rows[0]
                if media.modify_date > row.SyncDate:
                    # photo has been edited in picasa - create new picasa
                    # entry - note we do not delete the old drive entry
                    # because this still exists and diverges from now on
                    # Todo IMPORTANT - this approach of using SyncDate WILL NOT
                    # todo pick up edited files on the first ever sync
                    # this is a bit shit - but I can find no correlation
                    #  between modify dates on picsasa and drive as yet.
                    print(
                        'file {} dates are out by {}, picasa date {}, drive '
                        'date {}, drive SyncDate {}'.format(
                            media.filename, media.modify_date - row.SyncDate,
                            media.modify_date, row.ModifyDate, row.SyncDate
                        ))
                    self.put_new_picasa_media(media)
                else:
                    # store link between album and drive file
                    self.p._db.put_album_file(self.album.id, row.Id)
                    self.set_album_dates(row.CreateDate)
            elif rows is None:
                # no match so this exists only in picasa
                self.put_new_picasa_media(media)
            else:
                self.multiple_match_count += 1
                print ('  WARNING multiple files match %s %s %s' %
                       (media.orig_name, media.modify_date, media.size))

    def complete_album(self):
        # write the album data down now we know the contents' date range
        row = LocalData.AlbumsRow.make(AlbumId=self.album.id,
                                       AlbumName=self.album.filename,
                                       StartDate=self.album_start_photo,
                                       EndDate=self.album_end_photo,
                                       SyncDate=Utils.date_to_string(
                                           datetime.now()))
        self.p._db.put_album(row)
        if self.album.modify_date > self.latest_download:
            self.latest_download = self.album.modify_date

    def complete_scan(self):
        # save the latest and earliest update times. We only do this if a
        # complete scan of all existing albums has completed because the order
        # of albums is a little randomized (possibly by photo content?)
        if not (self.p.album_name or self.p.startDate or self.p.endDate):
            self.p._db.set_scan_dates(picasa_last_date=self.latest_download)
