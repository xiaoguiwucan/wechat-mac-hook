import unittest

from web_admin.server import declared_member_count, member_id_is_real, quoted_member_pairs, readable_group_name


class GroupDirectoryTests(unittest.TestCase):
    def test_real_group_name_rejects_internal_id(self):
        gid = "18725461928@chatroom"
        self.assertEqual(readable_group_name(gid, gid), "")
        self.assertEqual(readable_group_name("PT站看片狂魔小群", gid), "PT站看片狂魔小群")

    def test_extracts_quoted_member_name_from_wechat_xml(self):
        raw = """<msg><appmsg><refermsg><fromusr>45952610277@chatroom</fromusr>
        <chatusr>cping19810311</chatusr><displayname>半盏·清欢</displayname>
        </refermsg></appmsg></msg>"""
        self.assertEqual(quoted_member_pairs(raw, "45952610277@chatroom"), {"cping19810311": "半盏·清欢"})

    def test_extracts_html_escaped_quoted_member(self):
        raw = "&lt;refermsg&gt;&lt;chatusr&gt;wxid_real123&lt;/chatusr&gt;&lt;displayname&gt;小王&lt;/displayname&gt;&lt;/refermsg&gt;"
        self.assertEqual(quoted_member_pairs(raw, "123456789@chatroom"), {"wxid_real123": "小王"})

    def test_member_count_uses_newest_credible_value(self):
        values = ["<msgsource><membercount>110</membercount></msgsource>", "<membercount>108</membercount>"]
        self.assertEqual(declared_member_count(values), 110)
        self.assertEqual(declared_member_count(["<membercount>999999</membercount>"]), 0)

    def test_synthetic_ids_are_not_members(self):
        self.assertFalse(member_id_is_real("AI"))
        self.assertFalse(member_id_is_real("onebot-log"))
        self.assertFalse(member_id_is_real("123456789@chatroom", "123456789@chatroom"))
        self.assertTrue(member_id_is_real("wxid_real123"))


if __name__ == "__main__":
    unittest.main()
